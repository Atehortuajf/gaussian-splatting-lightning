from typing import Tuple, Optional, Any, List, Dict, Mapping
from dataclasses import dataclass, field
import lightning
import torch
from torch import nn
from gsplat.sh import spherical_harmonics

from . import RendererOutputInfo, RendererOutputTypes
from .renderer import Renderer, RendererConfig
from .gsplat_renderer import GSPlatRenderer, DEFAULT_ANTI_ALIASED_STATUS
from internal.utils.network_factory import NetworkFactory
from ..cameras import Camera
from ..models.gaussian import GaussianModel
from internal.encodings.positional_encoding import PositionalEncoding


@dataclass
class ModelConfig:
    n_gaussian_feature_dims: int = 64
    n_appearances: int = -1
    n_appearance_embedding_dims: int = 32
    is_view_dependent: bool = False
    n_view_direction_frequencies: int = 4
    n_neurons: int = 64
    n_layers: int = 3
    skip_layers: List[int] = field(default_factory=lambda: [])

    normalize: bool = False


@dataclass
class OptimizationConfig:
    gamma_eps: float = 1e-6

    embedding_lr_init: float = 2e-3
    embedding_lr_final_factor: float = 0.1
    lr_init: float = 1e-3
    lr_final_factor: float = 0.1
    eps: float = 1e-15
    max_steps: int = 30_000
    warm_up: int = 4000


class Model(nn.Module):
    def __init__(self, config: ModelConfig):
        super().__init__()
        self.config = config
        self._setup()

    def _setup(self):
        self.embedding = nn.Embedding(
            num_embeddings=self.config.n_appearances,
            embedding_dim=self.config.n_appearance_embedding_dims,
        )
        n_input_dims = self.config.n_gaussian_feature_dims + self.config.n_appearance_embedding_dims
        if self.config.is_view_dependent is True:
            self.view_direction_encoding = PositionalEncoding(3, self.config.n_view_direction_frequencies)
            n_input_dims += self.view_direction_encoding.get_output_n_channels()
        self.network = NetworkFactory(tcnn=False).get_network_with_skip_layers(
            n_input_dims=n_input_dims,
            n_output_dims=3,
            n_layers=self.config.n_layers,
            n_neurons=self.config.n_neurons,
            activation="ReLU",
            output_activation="Sigmoid",
            skips=self.config.skip_layers,
        )

    def forward(self, gaussian_features, appearance, view_dirs):
        appearance_embeddings = self.embedding(appearance.reshape((-1,))).repeat(gaussian_features.shape[0], 1)
        if self.config.normalize:
            gaussian_features = torch.nn.functional.normalize(gaussian_features, dim=-1)
            appearance_embeddings = torch.nn.functional.normalize(appearance_embeddings, dim=-1)
        input_tensor_list = [gaussian_features, appearance_embeddings]
        if self.config.is_view_dependent is True:
            input_tensor_list.append(self.view_direction_encoding(view_dirs))
        network_input = torch.concat(input_tensor_list, dim=-1)
        return self.network(network_input)


@dataclass
class GSplatAppearanceEmbeddingRenderer(RendererConfig):
    anti_aliased: bool = DEFAULT_ANTI_ALIASED_STATUS
    filter_2d_kernel_size: float = 0.3
    model: ModelConfig = field(default_factory=lambda: ModelConfig())
    optimization: OptimizationConfig = field(default_factory=lambda: OptimizationConfig())

    def instantiate(self, *args, **kwargs) -> "GSplatAppearanceEmbeddingRendererModule":
        if getattr(self, "model_config", None) is not None:
            # checkpoint generated by previous version
            self.model = self.model_config
            self.optimization = self.optimization_config

        return GSplatAppearanceEmbeddingRendererModule(
            anti_aliased=self.anti_aliased,
            model=self.model,
            optimization=self.optimization,
            filter_2d_kernel_size=self.filter_2d_kernel_size,
        )


class GSplatAppearanceEmbeddingRendererModule(Renderer):
    """
    rgb = f(point_features, appearance_embedding, view_direction)
    """

    def __init__(
            self,
            anti_aliased: bool,
            model: ModelConfig,
            optimization: OptimizationConfig,
            filter_2d_kernel_size: float,
    ):
        super().__init__()
        self.anti_aliased = anti_aliased  # tell absgrad whether AA enabled
        self.model_config = model
        self.optimization_config = optimization
        self.filter_2d_kernel_size = filter_2d_kernel_size

    def setup(self, stage: str, lightning_module=None, *args: Any, **kwargs: Any) -> Any:
        if lightning_module is not None:
            if self.model_config.n_appearances <= 0:
                max_input_id = 0
                appearance_group_ids = lightning_module.trainer.datamodule.dataparser_outputs.appearance_group_ids
                if appearance_group_ids is not None:
                    for i in appearance_group_ids.values():
                        if i[0] > max_input_id:
                            max_input_id = i[0]
                n_appearances = max_input_id + 1
                self.model_config.n_appearances = n_appearances

            self._setup_model()
            print(self.model)

    def _setup_model(self, device=None):
        self.model = Model(self.model_config)
        self.renderer = GSPlatRenderer()

        if device is not None:
            self.model.to(device=device)
            self.renderer.to(device=device)

    def load_state_dict(self, state_dict: Mapping[str, Any], strict: bool = True):
        self.model_config.n_appearances = state_dict["model.embedding.weight"].shape[0]
        self._setup_model(device=state_dict["model.embedding.weight"].device)
        return super().load_state_dict(state_dict, strict)

    def training_setup(self, module: lightning.LightningModule):
        embedding_optimizer, embedding_scheduler = self._create_optimizer_and_scheduler(
            self.model.embedding.parameters(),
            "embedding",
            lr_init=self.optimization_config.embedding_lr_init,
            lr_final_factor=self.optimization_config.lr_final_factor,
            max_steps=self.optimization_config.max_steps,
            eps=self.optimization_config.eps,
            warm_up=self.optimization_config.warm_up,
        )
        network_optimizer, network_scheduler = self._create_optimizer_and_scheduler(
            self.model.network.parameters(),
            "embedding_network",
            lr_init=self.optimization_config.lr_init,
            lr_final_factor=self.optimization_config.lr_final_factor,
            max_steps=self.optimization_config.max_steps,
            eps=self.optimization_config.eps,
            warm_up=self.optimization_config.warm_up,
        )

        return [embedding_optimizer, network_optimizer], [embedding_scheduler, network_scheduler]

    def preprocess(self, pc, viewpoint_camera, scaling_modifier):
        return GSPlatRenderer.project(
            means3D=pc.get_xyz,
            scales=pc.get_scaling,
            rotations=pc.get_rotation,
            viewpoint_camera=viewpoint_camera,
            scaling_modifier=scaling_modifier,
            extra_projection_kwargs={
                "filter_2d_kernel_size": self.filter_2d_kernel_size,
            },
        ), pc.get_opacities()

    def forward(self, viewpoint_camera: Camera, pc: GaussianModel, bg_color: torch.Tensor, scaling_modifier=1.0, render_types=None, **kwargs):
        if render_types is None:
            render_types = ["rgb"]

        projection_results, opacities = self.preprocess(pc=pc, viewpoint_camera=viewpoint_camera, scaling_modifier=scaling_modifier)

        xys, depths, radii, conics, comp, num_tiles_hit, cov3d = projection_results
        is_gaussian_visible = radii > 0

        if self.anti_aliased:
            opacities = opacities * comp[:, None]

        rgb = None
        if "rgb" in render_types:
            radii = projection_results[2]

            detached_xyz = pc.get_xyz.detach()
            view_directions = detached_xyz[is_gaussian_visible] - viewpoint_camera.camera_center  # (N, 3)
            view_directions = view_directions / view_directions.norm(dim=-1, keepdim=True)
            base_rgb = spherical_harmonics(pc.active_sh_degree, view_directions, pc.get_features[is_gaussian_visible]) + 0.5
            rgb_offset = self.model(pc.get_appearance_features()[is_gaussian_visible], viewpoint_camera.appearance_id, view_directions) * 2 - 1.
            rgbs = torch.zeros((radii.shape[0], 3), dtype=projection_results[0].dtype, device=radii.device)
            rgbs[is_gaussian_visible] = torch.clamp(base_rgb + rgb_offset, min=0., max=1.)

            rgb = self.renderer.rasterize_simplified(
                project_results=projection_results,
                viewpoint_camera=viewpoint_camera,
                colors=rgbs,
                bg_color=bg_color,
                opacities=opacities,
                anti_aliased=False,  # already applied AA above
            )

        inverse_depth_im = None
        if "inverse_depth" in render_types:
            inverse_depth = 1. / (depths.clamp_min(0.) + 1e-8).unsqueeze(-1)
            inverse_depth_im = self.renderer.rasterize_simplified(
                project_results=projection_results,
                viewpoint_camera=viewpoint_camera,
                colors=inverse_depth,
                bg_color=torch.zeros((1,), dtype=torch.float, device=bg_color.device),
                opacities=opacities,
                anti_aliased=False,  # already applied AA above
            )

        hard_inverse_depth_im = None
        if "hard_inverse_depth" in render_types:
            inverse_depth = 1. / (depths.clamp_min(0.) + 1e-8).unsqueeze(-1)
            hard_inverse_depth_im = self.renderer.rasterize_simplified(
                project_results=projection_results,
                viewpoint_camera=viewpoint_camera,
                colors=inverse_depth,
                bg_color=torch.zeros((1,), dtype=torch.float, device=bg_color.device),
                opacities=opacities + (1 - opacities.detach()),
                anti_aliased=False,  # already applied AA above
            )

        return {
            "render": rgb,
            "inverse_depth": inverse_depth_im,
            "hard_inverse_depth": hard_inverse_depth_im,
            "viewspace_points": xys,
            "viewspace_points_grad_scale": 0.5 * torch.tensor([[viewpoint_camera.width, viewpoint_camera.height]]).to(xys),
            "visibility_filter": is_gaussian_visible,
            "radii": radii,
        }

    def training_forward(self, step: int, module: lightning.LightningModule, viewpoint_camera: Camera, pc: GaussianModel, bg_color: torch.Tensor, scaling_modifier=1.0, **kwargs):
        if step < self.optimization_config.warm_up:
            return self.renderer(
                viewpoint_camera,
                pc,
                bg_color,
                scaling_modifier,
                **kwargs,
            )

        return self.forward(viewpoint_camera, pc, bg_color, scaling_modifier, **kwargs)

    @staticmethod
    def _create_optimizer_and_scheduler(
            params,
            name,
            lr_init,
            lr_final_factor,
            max_steps,
            eps,
            warm_up,
    ) -> Tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LRScheduler]:
        optimizer = torch.optim.Adam(
            params=[
                {"params": list(params), "name": name}
            ],
            lr=lr_init,
            eps=eps,
        )
        scheduler = torch.optim.lr_scheduler.LambdaLR(
            optimizer=optimizer,
            lr_lambda=lambda iter: lr_final_factor ** min(max(iter - warm_up, 0) / max_steps, 1),
            verbose=False,
        )

        return optimizer, scheduler

    def get_available_outputs(self) -> Dict[str, RendererOutputInfo]:
        return {
            "rgb": RendererOutputInfo("render"),
            "inverse_depth": RendererOutputInfo("inverse_depth", type=RendererOutputTypes.GRAY),
            "hard_inverse_depth": RendererOutputInfo("hard_inverse_depth", type=RendererOutputTypes.GRAY),
        }


# With MipSplatting version

@dataclass
class GSplatAppearanceEmbeddingMipRenderer(GSplatAppearanceEmbeddingRenderer):
    filter_2d_kernel_size: float = 0.1

    def instantiate(self, *args, **kwargs) -> "GSplatAppearanceEmbeddingMipRendererModule":
        return GSplatAppearanceEmbeddingMipRendererModule(
            anti_aliased=self.anti_aliased,
            model=self.model,
            optimization=self.optimization,
            filter_2d_kernel_size=self.filter_2d_kernel_size,
        )


class GSplatAppearanceEmbeddingMipRendererModule(GSplatAppearanceEmbeddingRendererModule):
    def __init__(self, anti_aliased: bool, model: ModelConfig, optimization: OptimizationConfig, filter_2d_kernel_size: float):
        super().__init__(anti_aliased, model, optimization, filter_2d_kernel_size)
        self.filter_2d_kernel_size = filter_2d_kernel_size

    def preprocess(self, pc, viewpoint_camera, scaling_modifier):
        opacities, scales = pc.get_3d_filtered_scales_and_opacities()
        return GSPlatRenderer.project(
            means3D=pc.get_xyz,
            scales=scales,
            rotations=pc.get_rotation,
            viewpoint_camera=viewpoint_camera,
            scaling_modifier=scaling_modifier,
            extra_projection_kwargs={
                "filter_2d_kernel_size": self.filter_2d_kernel_size,
            }
        ), opacities

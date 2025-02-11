import add_pypath
import os
import traceback
if "OMP_NUM_THREADS" not in os.environ:
    os.environ["OMP_NUM_THREADS"] = "4"
from internal.entrypoints.gs2d_mesh_extraction import main


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(traceback.format_exc())
        raise e

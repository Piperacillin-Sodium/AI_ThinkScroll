r"""Run the local HTML workbench: .venv\Scripts\python.exe run_web.py"""
import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="AI思卷 网页工作台")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    os.chdir(root)  # Legacy adapters log and resolve a few resources relative to cwd.
    try:
        import uvicorn
        from backend.app import create_app
    except ImportError as exc:
        raise SystemExit("缺少网页依赖。请先运行 .venv\\Scripts\\python.exe -m pip install -r requirements-web.txt") from exc
    print(f"AI思卷 工作台：http://127.0.0.1:{args.port}", flush=True)
    uvicorn.run(create_app(root), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()

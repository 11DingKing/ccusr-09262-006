"""本地启动入口：python -m service_09252_010.serve [--host 127.0.0.1] [--port 8080] [--db PATH]"""
from __future__ import annotations

import argparse

from wsgiref.simple_server import make_server

from .interfaces.wsgi_app import make_app


def main() -> None:
    parser = argparse.ArgumentParser(description="国际职教合作成效核算服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=None, help="SQLite 文件路径，默认临时目录")
    args = parser.parse_args()

    app = make_app(args.db)
    with make_server(args.host, args.port, app) as httpd:
        print(f"服务已启动：http://{args.host}:{args.port}  数据库：{app.container.db.path}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()

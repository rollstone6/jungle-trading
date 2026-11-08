#!/usr/bin/env python3
"""启动脚本"""
import os
import subprocess
import sys

def main():
    pwd = os.environ.get("SITE_PASSWORD", "")
    print("🚀 启动 持仓工作台...")
    print(f"📍 访问地址: http://localhost:8090/?pwd={pwd}")
    print(f"📊 维护模式: http://localhost:8090/?pwd={pwd}&update=1")
    print()
    
    subprocess.run([
        sys.executable, "-m", "uvicorn",
        "app.main:app",
        "--host", "0.0.0.0",
        "--port", "8090",
        "--reload"
    ])

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""启动脚本"""
import subprocess
import sys

def main():
    print("🚀 启动 Jungle 天才交易员持仓工作台...")
    print("📍 访问地址: http://localhost:8090/?pwd=0mGecaPX3duCfVXhEb")
    print("📊 维护模式: http://localhost:8090/?pwd=0mGecaPX3duCfVXhEb&update=1")
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

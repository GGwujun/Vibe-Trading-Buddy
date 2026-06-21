#!/bin/bash
# Vibe Trading 部署脚本
# 服务器: 47.115.144.24
# 用户: root

echo "========================================="
echo "Vibe Trading 自动部署"
echo "========================================="
echo ""

# 1. 登录阿里云 ACR
echo "步骤 1: 登录阿里云 ACR..."
docker login --username=876337269@qq.com --password=Gao876337@ crpi-i6pwsm2rbcu2h5uv.cn-shenzhen.personal.cr.aliyuncs.com

if [ $? -ne 0 ]; then
    echo "❌ ACR 登录失败"
    exit 1
fi
echo "✅ ACR 登录成功"
echo ""

# 2. 查找项目目录
echo "步骤 2: 查找项目目录..."
PROJECT_DIR=$(find /root /home -name "Vibe-Trading" -type d 2>/dev/null | head -1)

if [ -z "$PROJECT_DIR" ]; then
    echo "❌ 未找到项目目录，请手动指定:"
    echo "   cd /path/to/Vibe-Trading"
    exit 1
fi

echo "✅ 找到项目目录: $PROJECT_DIR"
cd "$PROJECT_DIR"
echo ""

# 3. 拉取最新镜像
echo "步骤 3: 拉取最新镜像..."
docker compose pull vibe-trading

if [ $? -ne 0 ]; then
    echo "❌ 镜像拉取失败"
    exit 1
fi
echo "✅ 镜像拉取成功"
echo ""

# 4. 重启服务
echo "步骤 4: 重启服务..."
docker compose up -d vibe-trading

if [ $? -ne 0 ]; then
    echo "❌ 服务启动失败"
    exit 1
fi
echo "✅ 服务启动成功"
echo ""

# 5. 查看服务状态
echo "步骤 5: 查看服务状态..."
docker compose ps
echo ""

# 6. 查看日志
echo "步骤 6: 查看最近日志 (Ctrl+C 退出)..."
docker compose logs vibe-trading --tail=50 --follow
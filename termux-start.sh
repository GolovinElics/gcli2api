echo "准备启动本地项目环境..."
uv sync
source .venv/bin/activate
pm2 start .venv/bin/python --name web -- web.py
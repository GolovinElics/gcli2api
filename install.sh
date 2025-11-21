apt update
apt install git -y
curl -Ls https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env.sh
if [ -f "./web.py" ]; then
    # Already in target directory; skip clone and cd
    :
elif [ -f "./gcli2api/web.py" ]; then
    cd ./gcli2api
fi
uv sync
source .venv/bin/activate
python3 web.py
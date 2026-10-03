# source scripts/gpu_env.sh（venv を有効にした後に）
# pip で入れた cuBLAS / cuDNN（nvidia-cublas-cu12 / nvidia-cudnn-cu12）の場所を LD_LIBRARY_PATH に足す。
# uv が作る .venv の activate はこれをしないので、faster-whisper が GPU を使うには必要。
# 何度 source しても同じパスを重ねて足さない。
_rt_gpu_libs="$(python -c "import nvidia.cublas.lib as a, nvidia.cudnn.lib as b; print(list(a.__path__)[0] + ':' + list(b.__path__)[0])")"
case ":${LD_LIBRARY_PATH:-}:" in
  *":${_rt_gpu_libs%%:*}:"*) ;;
  *) export LD_LIBRARY_PATH="${_rt_gpu_libs}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ;;
esac
unset _rt_gpu_libs

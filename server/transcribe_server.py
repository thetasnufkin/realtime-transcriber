"""transcribe_server.py  (互換シム)

本体は src/realtime_transcriber/ に移った。既存の start.bat から
`python server/transcribe_server.py ...` で起動できるよう、ここから呼び出す。
新しい起動方法: python -m realtime_transcriber ...
"""

from realtime_transcriber.cli import main

if __name__ == "__main__":
    main()

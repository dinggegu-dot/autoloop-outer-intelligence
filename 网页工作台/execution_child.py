"""先等待父进程登记进程组，再启动模型；登记前断开则退出。"""
import os
import sys

if __name__ == '__main__':
    # 必须直接读一个字节，避免缓冲吞掉随后交给模型的任务正文。
    if os.read(0, 1) != b'1' or len(sys.argv) < 2:
        raise SystemExit(75)
    os.execv(sys.argv[1], sys.argv[1:])

"""把 parquet 题库转成 jsonl（只需要运行一次）。观者自在本身只用标准库，读不了 parquet，所以单独放这个脚本。

用法（在 Anaconda Prompt 里）：
    python parquet转JSONL.py 题库.parquet
会在同一文件夹生成 题库.jsonl，之后在建题窗口里导入这个 jsonl 即可。
需要 pandas 和 pyarrow（Anaconda 默认都带；没有的话：pip install pandas pyarrow）。
"""
import json
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        sys.exit("用法：python parquet转JSONL.py 题库.parquet")
    src = Path(sys.argv[1])
    if not src.exists():
        sys.exit(f"找不到文件：{src}")
    try:
        import pandas as pd
    except ImportError:
        sys.exit("没有安装 pandas：请在 Anaconda Prompt 里运行 pip install pandas pyarrow")
    df = pd.read_parquet(src)
    out = src.with_suffix(".jsonl")
    with open(out, "w", encoding="utf-8") as f:
        for rec in df.to_dict(orient="records"):
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    print(f"共 {len(df)} 行，列：{list(df.columns)}\n已生成：{out}")


if __name__ == "__main__":
    main()

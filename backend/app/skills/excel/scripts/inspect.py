# app/skills/excel/scripts/inspect.py
# 看 Excel 表结构：sheet 清单 + 每个 sheet 的列名、样本值、行数。
# 样本值是刻意返回的：用户说的「手机号」在表里可能叫「联系电话」，只给列名模型只能猜，
# 给两个样本值它就能认出来。截断上限是防宽表（几十列、长文本单元格）撑爆模型上下文。
# 沙箱内执行：只 import 标准库 + openpyxl，不能 import 项目代码。
import itertools
import json
import os
import sys

MAX_COLUMNS = 30
PREVIEW_ROWS = 5
SAMPLE_ROWS = 3
SAMPLE_CHARS = 32


def _text(value):
    """单元格值转成短的、可读的字符串：None 当空串，长文本截断。"""
    if value is None:
        return ""
    s = str(value).strip()
    return f"{s[:SAMPLE_CHARS]}…" if len(s) > SAMPLE_CHARS else s


def _raw(value):
    """参数用字符串：只去空白。

    **不能走 _text**：那个带 SAMPLE_CHARS 显示截断。文件名比旧版的 uuid 更容易踩到这条——
    中文名一个字 3 字节，一个像样的中文文件名轻松超过 32 字符，截一刀就成了另一个不存在的名字。
    """
    return "" if value is None else str(value).strip()


def _work_files(work_dir):
    """本次执行 work_dir 里实际有哪些文件——模型填的名字对不上时用它自纠。

    这里只有后端为本轮塞进来的输入件，不含跨用户信息，可以放心回给模型。
    desensitize.py 里有一份同样的：沙箱只执行单个脚本的源码，脚本之间无法 import。
    """
    try:
        return "、".join(sorted(os.listdir(work_dir))) or "空"
    except OSError:
        return "读不到"


def _resolve(args, work_dir):
    """定位输入文件：后端给的是本次执行 work_dir 下的裸文件名。

    **不做绝对路径分支**：那正是路径穿越的入口（模型填 `/etc/passwd` 就会被原样打开）。
    统一 basename 后拼进 work_dir，落点必然在本次执行目录内。
    """
    ref = _raw(args.get("file"))
    if not ref:
        raise SystemExit(f"缺少 file：把会话文件清单里的文件名原样填进来（本次可用：{_work_files(work_dir)}）")
    return os.path.join(work_dir, os.path.basename(ref))


def summarize_sheet(name, preview_rows, total_rows, header_row=1):
    """整理一个 sheet 的结构摘要（纯函数，便于单测）。

    preview_rows 是 sheet 开头若干行（含表头行本身，行号从 1 算起），total_rows 是整表行数。
    """
    head = max(0, header_row - 1)
    header = [_text(c) for c in preview_rows[head]] if head < len(preview_rows) else []
    # 空表头列用位置兜底命名：脱敏要按列名指定目标列，无名列也得有个能指认的名字
    columns = [h or f"第{i + 1}列" for i, h in enumerate(header)][:MAX_COLUMNS]
    samples = {}
    for i, col in enumerate(columns):
        values = []
        for row in preview_rows[head + 1:]:
            v = _text(row[i]) if i < len(row) else ""
            if v and v not in values:
                values.append(v)
            if len(values) >= SAMPLE_ROWS:
                break
        samples[col] = values
    return {"name": name, "row_count": total_rows, "header_row": header_row, "columns": columns,
            "samples": samples, "preview": [[_text(c) for c in row] for row in preview_rows]}


def main():
    payload = json.load(sys.stdin)
    args, work_dir = payload["args"], payload["work_dir"]

    from openpyxl import load_workbook  # 延迟导入：纯逻辑单测不必装 openpyxl

    path = _resolve(args, work_dir)
    if not os.path.exists(path):
        # 列出实际有哪些：模型按清单填了名字却对不上时（比如把 file_id 当名字填进来），一轮就能自纠
        raise SystemExit(f"读不到文件：{os.path.basename(path)}（本次可用：{_work_files(work_dir)}）")

    header_row = int(args.get("header_row") or 1)
    # read_only：只读结构，不把整表载入内存（宽表能到几十万行）
    wb = load_workbook(path, read_only=True, data_only=True)
    sheets = [summarize_sheet(ws.title,
                              [[c.value for c in row] for row in itertools.islice(ws.iter_rows(), PREVIEW_ROWS)],
                              ws.max_row or 0, header_row)
              for ws in wb.worksheets]
    wb.close()
    print(json.dumps({"sheets": sheets}, ensure_ascii=False))


if __name__ == "__main__":
    main()

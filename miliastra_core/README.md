# `miliastra_core` 架构说明

`miliastra_core` 是与界面无关的图片处理和 GIA 导出核心，同时保留只读 GIL 检查能力。依赖方向固定为：

```text
UI / scripts
    |
    v
miliastra_core.raster  --->  miliastra_core.export
                                    |
                                    v
                         miliastra_core.protobuf

scripts/inspect_gil.py ---> miliastra_core.gil ---> miliastra_core.protobuf
```

## 模块边界

| 模块 | 职责 | 主要公共入口 |
| --- | --- | --- |
| `raster` | 图片读取、缩放、背景过滤、颜色量化和矩形合并 | `build_raster_plan`、`RasterAlgorithmSettings` |
| `image` | UI 无关的图片转 GIA 工作流及旧版参数兼容 | `ImageGiaSettings`、`build_image_gia_bytes` |
| `sketch` | 线稿检测、骨架提取、曲线拟合及 GIA 构建 | `SketchGiaSettings`、`analyze_sketch` |
| `sketch_config` | 线稿处理配置模型与默认参数 | `SketchProcessingConfig` |
| `export` | 把 `RasterPlan` 映射为 GIA 对象，以及底层 GIA 二进制构建 | `build_gia_from_plan`、`build_gia` |
| `gil` | 只读检查 GIL 容器、实体和装饰物关系 | `GilDocument` |
| `protobuf` | 无损解析和重建 Protobuf wire 字段 | `parse_fields`、`rebuild_message` |
| `_io` | 核心包内部的原子文件写入 | 非公共 API |

## 设计约束

- 核心包不能导入 `UI`、Streamlit 或任务队列代码。
- UI 页面直接依赖 core，不在 `UI` 中保留业务模块副本或兼容转发文件。
- 栅格缓存键只由源图和算法参数决定，不得包含导出参数。
- 未修改的 Protobuf 字段必须保留原始字节，包含未知字段和非规范 varint。
- Tools 不提供 GIL 导出能力；GIL 模块只用于检查现有文件。
- Tools 内不保存或加载 `.proto`、descriptor 文件；运行时不得依赖 Tools 目录外的文件。
- 新的公共符号必须加入对应子包的 `__all__`，内部辅助模块不从顶层导出。

## 验证

在仓库根目录运行：

```powershell
python -m pytest -q --basetemp .pytest-tmp
python -m compileall -q miliastra_core
streamlit run .\streamlit_app.py
```

"""pytest 配置。

tests/ 下的每个文件都是**可独立运行的脚本**（python3 tests/xxx.py），
末尾用 sys.exit(1) 表达失败。pytest 收集它们时会在 import 阶段就执行
sys.exit ⇒ 整个 pytest 进程 INTERNALERROR，一个测试也跑不了。

这里把它们排除出 pytest 的收集范围；需要跑就逐个人工执行。
将来若要改成正式 pytest 用例，删掉对应的 ignore 项即可。
"""

collect_ignore = [
    "test_client.py",
    "test_client_bulk_offline.py",
    "test_config.py",
    "test_extract.py",
    "test_proxy_offline.py",
    "test_routes.py",
    "test_upload_node.py",
    "test_video_integration.py",
]

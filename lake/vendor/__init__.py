"""lake.vendor —— vendored 第三方库（避免 pip 依赖漂移）。

v6.1：easy_tdx 从 GitHub src vendor 进仓库（researcher 已验证可独立运行；
33★ 个人项目，vendor 防作者停更/接口漂移——pip 版本锁定挡不住上游破坏性变更）。
用法：adapter 内 sys.path 注入本目录后按原包名 import（等价 site-packages 安装）。
"""

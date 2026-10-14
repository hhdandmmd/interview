# 第三方来源与许可证

项目自写源码使用MIT许可。第三方组件保留各自许可；发布包`licenses`保存构建环境实际分发的许可文件。运行依赖为NumPy、Pandas、PyArrow、Requests、Filelock及其锁定依赖，CPython、PyInstaller运行支持及随包二进制库亦按各自条款分发。

真实数据来源为东方财富公开财报接口；请求、观察时间、原始HTTP正文和SHA-256随归档保留。独立参考来自茅台官方网站、巨潮资讯网的公开公司报告。项目许可证不改变上游金融数据或原报告的权利归属。离线样例用于面试复现和数据核对，人工夹具单独标识。

小范围后复权日线来自腾讯公开接口，接口形态参考AKShare维护者的公开适配代码；本项目未复制其实现或引入AKShare依赖。行情来源、请求范围、观察与字节摘要保留，不改变上游数据权利归属。`E:\backtest`只作设计思路参考，未复制其代码或私有数据。

旧探测脚本、历史设计和评审保留为过程证据。构建初期Anaconda系统包与PyInstaller混用失败，最终Windows构建改为独立CPython3.13虚拟环境，未修改全局Python或第三方源码。PyInstaller使用方法依据[官方文档](https://pyinstaller.org/en/stable/usage.html)，打包资源和依赖钩子依据[官方说明](https://pyinstaller.org/en/stable/hooks.html)。

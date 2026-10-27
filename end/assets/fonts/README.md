# PDF 字体

`NotoSansCJKsc-Regular.otf` 来自 [Noto CJK](https://github.com/notofonts/noto-cjk/tree/main/Sans/OTF/SimplifiedChinese)，用于 PDF 内嵌简体中文字体。许可证为 SIL Open Font License 1.1，全文见 `OFL.txt`。

下载来源：
https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf

部署时保留该字体与许可证；PDFKit 会仅嵌入文档使用的字形，运行时不需要 macOS 打印工具或在线下载字体。

字体文件 SHA-256：`2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b`

> **字体不随仓库分发。** 它单个 16 MB，而 `end/` 已冻结为只读参考、CI 也不再构建它，所以从索引里移除了（`.gitignore` 已忽略 `end/assets/fonts/*.otf`）。要运行旧后端，按上面的下载来源把文件放回本目录即可；校验 SHA-256 一致即与原版相同。


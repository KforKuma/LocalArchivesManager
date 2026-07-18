# Third-party notices

LAM 0.6.1 on Windows is distributed as a PyInstaller onedir application. The
release asset manifests record the exact model and Poppler files included in a
particular staging tree.

## Material runtime components

| Component | License | Project/source |
|---|---|---|
| EasyOCR and the staged English model files | Apache-2.0 | https://github.com/JaidedAI/EasyOCR |
| PyTorch | BSD-3-Clause | https://github.com/pytorch/pytorch |
| torchvision | BSD-3-Clause | https://github.com/pytorch/vision |
| OpenCV | Apache-2.0 | https://github.com/opencv/opencv |
| Poppler | GPL-2.0-or-later | https://poppler.freedesktop.org/ |

Poppler's complete staged dependency and license-file inventory is recorded in
`vendor/poppler/manifest.json`; copied license texts are under
`vendor/poppler/licenses/`. EasyOCR model origins, sizes and SHA-256 values are
recorded in `models/easyocr/manifest.json`, with the EasyOCR license copied next
to the model files.

The frozen `_internal/` directory also contains Python packages and native
libraries selected by PyInstaller's official hooks and hooks-contrib. Their
license metadata remains included when supplied by the upstream package. A
release candidate must preserve all license files and must complete a legal
review of GPL obligations before external redistribution.

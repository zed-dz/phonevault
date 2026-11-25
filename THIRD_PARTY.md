# Third-party software

Nothing is bundled in this repository. pip installs these from PyPI:

| Package | Licence | Link |
|---|---|---|
| fastapi | MIT | https://github.com/fastapi/fastapi |
| starlette (through fastapi) | BSD-3-Clause | https://github.com/Kludex/starlette |
| pydantic (through fastapi) | MIT | https://github.com/pydantic/pydantic |
| uvicorn | BSD-3-Clause | https://github.com/Kludex/uvicorn |
| python-multipart | Apache-2.0 | https://github.com/Kludex/python-multipart |
| pillow | MIT-CMU | https://github.com/python-pillow/Pillow |
| rich | MIT | https://github.com/Textualize/rich |

For the tests only: pytest (MIT), httpx (BSD-3-Clause).

Programs you may have, started as separate programs and never shipped here:

- adb, from [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools), under Google's Android SDK terms. Needed for a real phone.
- [ffmpeg](https://ffmpeg.org/legal.html) (LGPL or GPL, depending on the build). Optional, for video thumbnails.

The banner, the social image, the demo recording and the sample pictures were made for this project and are MIT like the code.

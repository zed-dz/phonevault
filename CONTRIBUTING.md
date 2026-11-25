# Contributing

```
git clone https://github.com/zed-dz/phonevault
cd phonevault
python -m venv .venv
.venv\Scripts\activate          # Linux and macOS: source .venv/bin/activate
pip install -e ".[test]"
python -m pytest -q
```

The tests never start adb and never open a phone. If a test tries, it fails.

Rules a change has to keep:

1. Backed-up files keep the same bytes as on the phone.
2. Nothing in the backup is hard-deleted. Removing means moving to the trash folder, with a journal row, so undo works.
3. Names and paths that come from the phone or over HTTP are input. Use `core/names.py`.
4. The dashboard stays on 127.0.0.1 unless the user asks, and every route needs the token.
5. Backup and undo never write to the phone. Only `restore` does.
6. A setting that acts without being asked is off by default.

`tests/test_promises.py`, `tests/test_no_loss.py` and `tests/test_security.py` hold these. A fix comes with a test that fails without it.

Reporting a phone that behaves oddly: say the model, the Android version, the link (USB adb, Wi-Fi adb, MTP) and what `phonevault status` prints. Cut out serial numbers and file names you do not want public.

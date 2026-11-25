# Security

Please report a problem in private: on GitHub, open the **Security** tab of this repository and press **Report a vulnerability**. Do not open a public issue for it. I answer as soon as I can, usually within a few days.

Only the latest release gets fixes.

What I care about most:

- reading or changing the backup without the token
- a web page that can make the dashboard do something
- a file name, setting, contact or app on the phone that writes outside the PC folder or runs a command
- any way to lose a file without a copy in the trash folder

Known and by design: the backup itself is not encrypted, and `--remote` is plain http. Anyone who can read your user profile can read the token in `config.json`.

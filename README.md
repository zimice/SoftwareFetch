# SoftwareFetch

Download official Windows installers into one folder for a new Windows 10/11 x64 PC. **It downloads files; it does not silently install, execute, or update any applications.** It supports parallel transfers, CLI selection, retries, basic file-signature validation, SHA-256 output, and a self-contained Windows EXE build.

## Get started on Windows (no Python required)

1. Upload this repository to GitHub (all source files, including `.github/workflows/build-windows.yml`).
2. Open **Actions → Build and test Windows executable → Run workflow**. You can also let the workflow run on a push to `main`/`master`.
3. Open the successful `windows-exe` run and download the **SoftwareFetch-Windows-x64** artifact. Extract it to a folder.
4. Double-click `Run-Windows.cmd` or run `SoftwareFetch.exe` in Terminal. The `install/` directory will be created beside the EXE.

The ZIP containing the source is **not** a prebuilt Windows EXE. GitHub Actions (or `Build-Windows.cmd` on your own Windows PC) produces the EXE using PyInstaller on Windows. The EXE bundles Python and `requests`. Windows SmartScreen can warn about an unsigned, independently built EXE; inspect/build the source yourself if desired.

## Run from Python instead

```powershell
py -3 -m pip install -r requirements.txt
py -3 SoftwareFetch.py
```

To build locally with Python installed, double-click `Build-Windows.cmd` and take the EXE, configuration, and launcher from `dist/`.

## Options

```powershell
SoftwareFetch.exe --list
SoftwareFetch.exe --apps "7zip" "Firefox" "Git Bash"
SoftwareFetch.exe --apps "VirtualBox"     # includes configured VC++ download dependency
SoftwareFetch.exe --workers 4 --dest D:\Installers
SoftwareFetch.exe --force
SoftwareFetch.exe --verify              # compare downloaded files with SHA256SUMS.txt
SoftwareFetch.exe --verbose
SoftwareFetch.exe --config custom.json
```

If the EXE is beside `config.json`, it reads that editable file. Otherwise it uses the default bundled configuration. A run creates/updates `SHA256SUMS.txt` for successfully processed files, including manual shortcuts; hashes **detect later changes, not whether a vendor is trustworthy**. `--verify` checks entries recorded by the most recent run. For full coverage, run a download of all configured apps first. Failed downloads remain visible in the console and return exit status 1; incorrect config/arguments return 2.

### Program coverage

| Program | Delivery | Notes |
|---|---|---|
| 7-Zip | EXE | Official website |
| Brave | EXE | Vendor endpoint |
| Bitwarden | EXE | Official Windows desktop installer |
| Firefox | EXE | 64-bit, en-US |
| MEGAsync | EXE | Official endpoint |
| Python | EXE | Latest Windows 64-bit installer linked from Python.org |
| Notion | EXE | Official endpoint |
| MobaXterm | ZIP | Free Home Edition installer package |
| Microsoft Office | `.url` shortcut | **Manual:** Microsoft account/Office licence and edition selection required |
| VS Code | EXE | Stable x64 user installer |
| Git Bash | EXE | Git for Windows stable GitHub release |
| KeePass | EXE | KeePass 2 standard installer |
| Rufus | EXE | Portable build from official stable GitHub release |
| Open Hardware Monitor | ZIP | Portable package |
| Tor Browser | EXE | Vendor-listed Windows x64 installer (vendor may temporarily list an older, signed version) |
| Discord | EXE | Official Windows installer |
| Steam | EXE | Official installer |
| DB Browser for SQLite | MSI | Official stable x64 GitHub release |
| VirtualBox | EXE | Official Windows host package |
| VLC | EXE | Latest win64 release |
| Wireshark | EXE | Stable x64, not development release |
| WinSCP | EXE | Official release |
| Visual C++ Redistributable x64 | EXE | Additional utility/dependency for VirtualBox |

**Office:** The script writes `install/Office.url` pointing to Microsoft's official Office download page. Office itself is not a single freely redistributable installer. Sign in to download your licensed product. For managed Microsoft 365 deployments, use Microsoft's [Office Deployment Tool](https://learn.microsoft.com/en-us/microsoft-365-apps/deploy/overview-office-deployment-tool).

**MobaXterm:** The Home Edition installer is provided as a ZIP. Extract it before running the setup. Check its licence for your use case.

**Tor Browser:** The official site can offer an older Windows installer while addressing code-signing problems. Follow the browser's automatic update after installation and verify its official signature when possible.

## Security and maintenance

- Sources are explicitly configured official vendor sites or their official GitHub release repositories; software is not mirrored in this repository.
- HTTP download responses must use HTTPS, be at least 1 KiB, not be HTML/JSON/plain text, and have the expected binary magic bytes. The download is first written to `.part` and atomically renamed when complete.
- These checks do **not** authenticate publisher code signatures. Verify signatures and publisher checksums before running downloaded installers, particularly security-related tools. A SHA-256 file generated by this project is **not** a vendor-issued checksum.
- The downloaders rely on the vendors' current HTML layouts, URLs, and published assets. Vendors can change them: failures are reported rather than silently treated as successful downloads.
- The project deliberately does not run downloaded installers as administrator.
- Edit `config.json` to disable an entry (`"enabled": false`), add entries, or change the download folder. A source can be `direct`, `page` (first matching anchor), `github_release` (latest stable asset), or `manual` (official `.url` shortcut).

## Test

```shell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Tests use mocked download responses and do not install any third-party program. The Windows EXE build is handled on Windows; source-only tests do not prove that all vendor download servers are accessible at runtime.

## Licence

The Python code and configuration in this repository are provided under the MIT License; third-party software has its own licence and is **not** included in the repository.

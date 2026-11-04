# Silent guided demo

[`omarchy-game-mode-demo.mp4`](../site/assets/omarchy-game-mode-demo.mp4) is a 56-second guided walkthrough with burned-in English text and no audio track. It shows the actual GTK application, not a drawn mockup. Steam/session state, installed-client availability and the controller indicator are fixtures; no gameplay, physical controller validation, client installation or live Steam handoff is depicted. The [live page](https://brianbaldock.github.io/omarchy-game-mode/#preview) includes a player; the MP4 can also be downloaded for a community post. Keep its disclosure when sharing.

Chapters: Home and Resume (0:00), Discover (0:07), platform details (0:14), pinning (0:21), automatic/manual modes (0:28), Arabic RTL (0:35), optional login/mapper controls (0:42), and the contributor invitation (0:49). The rendered UI runs with isolated in-memory settings and no live device or compositor integration.

To reproduce, use the system GTK/PyGObject dependencies plus `ffmpeg`, Liberation Sans, `gtk4-broadwayd` and a browser. From the repository root, start `gtk4-broadwayd --address=127.0.0.1 :23`, connect a browser to `http://127.0.0.1:8103/` with a viewport at least 1280 by 720, then run:

```bash
GDK_BACKEND=broadway BROADWAY_DISPLAY=:23 python3 scripts/render-demo.py site/assets/omarchy-game-mode-demo.mp4
```

Stop the renderer and browser when finished. The demo intentionally does not change your real settings, start clients, install software, enable login startup or touch controllers. The renderer reuses `tests/ui_smoke.py`; its fixture availability must not be presented as tested platform or hardware support. Do not remove the disclosure when sharing.

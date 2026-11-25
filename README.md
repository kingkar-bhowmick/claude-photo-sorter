# Photo Sorter

Sort a camera roll into folders with Codex, review possible duplicates, and watch the progress in a dashboard on your computer.

![The dashboard sorting a camera roll](docs/preview.gif)

<sub>The demo above uses drawn placeholder images, not real photos.</sub>

- **Sorts photos and videos** into folders like Selfies, Food, Travel, Screenshots, Talking heads and Screen recordings. Use the standard folders or make your own.
- **Finds duplicates**: identical copies, burst shots and re-saved screenshots go to a `_Review` folder, with a list of which copy was kept.
- **Makes sorted copies.** Your originals stay where they are. macOS can make space-saving copies on the same APFS volume.
- **Runs on your computer.** The labeler receives small previews.

## Start here (no coding needed)

The default labeler uses the Codex CLI and a ChatGPT account with Codex access. Windows and Linux also work, with fewer folder-picker conveniences.

**1. Install the [Codex CLI](https://developers.openai.com/codex/cli).** Open Terminal, run `codex login`, and sign in with ChatGPT.

**2. Paste this to Codex:**

```
Set up and open https://github.com/kairothq/claude-photo-sorter
```

Codex downloads it, sets it up and opens the dashboard in your browser.

**3. In the dashboard:**

1. **Choose folder**: pick the folder with your photos (a Google Photos or Apple Photos export, or any folder).
2. Pick where the sorted copies should go. The default is right next to your photos.
3. Choose **Camera Roll**, **Instagram Reels**, or **Cinematic Editing**. Select **Add or edit labels** to name your own folders and describe what goes in them.
4. Press **Start sorting**.

The dashboard estimates the time and shows progress as labels come in and copies are made.

<details>
<summary>Don't have your photos on your computer yet?</summary>

- **Google Photos:** go to [takeout.google.com](https://takeout.google.com), select only Google Photos, download, and unzip everything into one folder.
- **iPhone or Apple Photos:** in Photos on your Mac, select all, then File > Export > Export Unmodified Originals, into a new folder.
</details>

## Using it again

The dashboard lives at **http://127.0.0.1:8765** while it's running. To open it again later, either:

- double-click **Open Photo Sorter.command** in the `claude-photo-sorter` folder (it's in your home folder), or
- tell Codex "open my photo sorter".

Press **New sort** to sort a different folder, or send the copies somewhere else.
Choose **Use last source and destination** to run the same pair again. The sorter checks for changes in the source and reuses Codex labels for unchanged files. Turn on **Sort from scratch** to scan the source again and generate new labels for the whole run.

- **Add to existing folders** keeps earlier copies in place, skips matching copies that are already there, and adds new files to the same category folders. Different files with the same name get distinct filenames.
- **Replace previous sorted copies** applies the current sort to the destination. It removes obsolete copies the sorter can verify against its report after writing the current copies. Manually added or modified files stay.

The dashboard shows the latest run. The destination's reports include earlier copies kept by **Add to existing folders**.

## The dashboard

| | |
|---|---|
| **Sort with Codex** | Asks for the source or destination if needed, then starts sorting. Afterward, **Replay sort** replays the animation. Add `?d=20` to the address for a slower take. |
| **New sort** | Pick a new photos folder and a new place for the copies. |
| **Stop sorting** | Stop the active run without closing the dashboard. **Resume sorting** reuses saved labels and retries an interrupted scan or copy. |
| **Sorting preset** | Choose the standard camera roll categories or group assets for Instagram Reels or Cinematic Editing. Each media file gets one folder in the selected preset. |
| **Advanced** (in the setup card) | Select Codex, Claude, or Jev, or try 200 files first. |
| **Click a folder** | Browse everything in it. Click a photo to see it full size, flip with the arrow keys, and **Move to** another folder if it's wrong. |
| **Edit labels** | Add, rename, or remove labels for the selected preset. |
| **Open sorted folder** | Opens the result in Finder. |
| ☀ / ☾ | Light or dark. |

Want a look without your own photos? Run `./start.sh --demo`.

## How it works

1. **Scan**: makes a small preview of every file and a fingerprint to spot copies. Your originals are only read.
2. **Duplicates**: compares fingerprints, plus a pixel check for screenshots so similar app screens aren't mistaken for copies.
3. **Labels**: Codex looks at one sheet of up to 30 previews per request, with two requests at a time. The sorter checks each label and saves valid answers so retries can pick up where they stopped.
4. **Folders**: copies each file into its folder, and duplicates into `_Review/duplicates`. Writes `sorted.csv` and `duplicates.csv`.

Claude remains available under **Advanced** for users signed in to Claude Code. [TypeSafe's Jev](https://typesafe.ai) requires `JEV_KEY` in a local `.env` file. It shows a token estimate and asks before running.

## Privacy

- The dashboard only listens on `127.0.0.1`, so nothing else on your network can reach it.
- Previews, labels and the file list stay in `dashboard/data/` and `work/`, which are never committed.
- Codex sends contact sheets of small previews to OpenAI using your ChatGPT sign-in and Codex allowance. Claude and Jev send previews to their own services. The original files stay on your computer.

## Running it by hand

```bash
./setup.sh          # once: Python packages into .venv/
./start.sh          # opens http://127.0.0.1:8765
```

Run `.venv/bin/python scripts/label_codex.py` after scanning to label from the command line, then run `.venv/bin/python scripts/apply.py "<output folder>" --labeler codex` to add the files. Pass `--output-mode replace` to replace the previous sorted copies. See `CLAUDE.md` for all commands. The sorter needs Python 3.9+ and ffmpeg for video previews (`brew install ffmpeg`).


## To Stop The Dashboard Running 


```bash
sorter_pid=$(lsof -tiTCP:8765 -sTCP:LISTEN)
if [[ -n "$sorter_pid" && "$(ps -p "$sorter_pid" -o args=)" == *scripts/server.py* ]]; then
  kill -TERM "$sorter_pid"
fi

Then verify

lsof -nP -iTCP:8765 -sTCP:LISTEN
```

## License

MIT

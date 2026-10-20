# Season Keeper

Season Keeper watches series you tag in Sonarr. It keeps season 1. When nobody has played the show for 90 days, it unmonitors every other season and deletes those episode files. When someone plays season 1, it monitors the later seasons and asks Sonarr to search for them.

Open the page at `http://<unraid-ip>:8787`. The first screen asks you to open **Admin** and enter Sonarr and Plex. The schedule stays in preview until you choose **Live** on that page and save. From the library page you can delete the later seasons of one show, check the result, and send that show back to Sonarr to download again.

## What the page shows

Each tagged show has:

- Season 1 episode count, file count, and size
- Later-season episode count, file count, and size
- The space a delete would free
- A season-by-season table

The totals at the top are the whole tagged library. "Rules would free" is only the shows the 90-day rule would actually remove. A show that was played inside the window stays, and its size is still listed on the card.

## Rules

Only series with the Sonarr tag `season-keeper` are managed. The app creates that tag.

Season 1 is never deleted. Add `0` to the keep list on the admin page if you also want to keep specials.

A show is active when Plex has a play of any episode in the last 90 days, or someone is watching it now. An active season 1 play monitors later seasons and searches the ones with missing aired episodes. Any other recent play leaves the files alone. No play in the window, and a complete Plex history, removes the later seasons.

If a show cannot be matched to Plex, or the Plex history scan stops early, nothing is deleted.

Matching uses the TVDB id Sonarr stores and the TVDB guid on the Plex show. A model is not used. A guess about two titles being the same show could delete the wrong series.

## Install on Unraid

The image is `ghcr.io/keberling/season-keeper:latest`. Sonarr and Plex are entered on the Admin page after the container is running. The template asks for the port, the appdata folder, and the container user.

Plex on Unraid is often on the host network, and Sonarr is often on a bridge port. From this container, `localhost` is the container itself. Use the Unraid server IP for both URLs.

1. Copy `unraid/my-season-keeper.xml` to the flash drive at `/boot/config/plugins/dockerMan/templates-user/my-season-keeper.xml`. From Windows that share is `\\unraid\flash\config\plugins\dockerMan\templates-user\`.
2. In Unraid, open Docker, then Add Container. Choose the `season-keeper` template.
3. Set the restart policy to Unless Stopped. Apply.
4. Open the WebUI. On Admin, enter the Sonarr URL and API key, and the Plex URL and server-owner token. Leave **Preview** selected. Save.
5. Press **Test connection**. Both sides should report OK.
6. In Sonarr, edit a series and add the tag `season-keeper`.
7. Open the library page and press **Refresh preview**.

Sonarr's API key is under Settings, General. The Plex token has to be the server owner's token so the server history includes every account.

Other people can add `https://github.com/keberling/season-keeper` under Apps, Settings, Template repositories. Season Keeper then shows up in the Apps tab from the same XML file.

To build on the server instead of pulling the image:

```bash
HOST_DATA_DIR=/mnt/user/appdata/season-keeper docker compose up -d --build
```

If this container already has Sonarr and Plex in its environment from an earlier setup, the first start copies them into `settings.json` in the data folder. After that, the admin page is what the app uses. Saving on the admin page applies immediately. The listen port, data folder, and PUID/PGID still come from the container. Change those in Unraid or Compose, then restart.

## Try one show

Preview mode does not stop you from testing a single series.

1. Open the show and read the episode counts and sizes.
2. Press **Test delete on this show**.
3. The dialog lists every season, including season 1, and the plan for each one. Check the box and confirm.
4. Sonarr unmonitors the later seasons and deletes those episode files. Season 1 stays.
5. Press **Download later seasons**. Confirm again. Sonarr monitors those seasons and searches for the missing aired episodes. Your existing indexers and download client do the downloading.

The schedule will not repeat that delete while the admin page is set to Preview.

## Turn the schedule on

When the preview looks right, open **Admin**, choose **Live**, check the confirm box, and save. The library banner switches from Preview to Live. The next scheduled read can delete later seasons of idle tagged shows and ask Sonarr to search when season 1 is played.

Choose **Preview** and save again to stop the schedule from changing Sonarr.

## If a show does not match

The card says "No Plex match". Refresh the show's metadata in Plex so it has a TVDB guid, or pin it. Copy `overrides.example.json` to the appdata folder as `overrides.json`:

```json
{
  "tvdbToPlexRatingKey": {
    "73244": "12345"
  }
}
```

The number on the left is the TVDB id from Sonarr. The number on the right is the Plex rating key. An unmatched show is never deleted by the schedule.

## Admin page

| Field | Default | Meaning |
| --- | --- | --- |
| Sonarr URL | | Sonarr base URL, including a URL base if you use one |
| Sonarr API key | | Sonarr API key. After the first save, leave this blank to keep it |
| Plex URL | | Plex URL, usually port 32400 |
| Plex token | | Server owner token. Leave blank after the first save to keep it |
| Preview / Live | Preview | Preview reads the libraries and changes nothing on the schedule |
| Watch window | 90 days | How long a play keeps the later seasons |
| Seasons to keep | 1 | Seasons that are never deleted. Season 1 is always included |
| Sonarr tag | season-keeper | Sonarr tag that opts a series in |
| Check every | 15 minutes | How often the schedule runs |
| Search cooldown | 6 hours | Minimum time between searches of the same season |
| Monitor new items while active | all | Sonarr's new-item setting while the show is active. `keep` leaves it alone |
| Monitor new items while idle | none | Same setting after the show goes idle |
| Access token | empty | When set, refresh, the per-show buttons, and the admin page need this token |

The admin page writes `settings.json` in the data folder. That file contains the API key and the Plex token. The pages show a short hint after save, and the full values stay in the file.

## Container

| Setting | Default | Meaning |
| --- | --- | --- |
| Host port | 8787 | Mapped to port 8787 in the container |
| `HOST_DATA_DIR` | `./data` | Folder mounted at `/data` |
| `PUID` / `PGID` | `99` / `100` | Account that owns the data folder |
| `TZ` | `UTC` | Timezone for container logs |

`.env.example` lists those container values for Compose. Copy it to `.env` when you want Compose to pick them up. Sonarr and Plex are entered in the browser.

## Logs

```bash
docker logs -f season-keeper
```

A delete line looks like `Deleted 22 episode file(s) for The Office (2005)`. In preview mode those lines are not written, because no files are deleted.

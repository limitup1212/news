# Lebanese news audio bulletin

Reads new stories from [Elnashra](https://www.elnashra.com/) and the [National News Agency](https://www.nna-leb.gov.lb/ar) aloud in Arabic as an MP3 and sends them to a Telegram chat.
GitHub Actions checks for free every 30 minutes, so no computer has to stay on. Each story is sent once, and nothing is sent when there are no new stories.

## Put it on GitHub

1. Sign in to the GitHub account that will own this project and create a new, empty **public** repository.
2. In this folder, run the commands below. Use that account's name and email, and its repository address:

   ```bash
   git config user.name "THEIR NAME"
   git config user.email "THEIR_EMAIL"
   git add .
   git commit -m "First version"
   git remote add origin https://github.com/THEIR_USERNAME/THEIR_REPO.git
   git push -u origin main
   ```

   Check that `config.json` is not in the list of committed files. It holds the Telegram token and is ignored on purpose.

## Turn on the bulletin

1. In the repository, open **Settings > Secrets and variables > Actions**.
2. On the **Secrets** tab, add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
3. Open the **Actions** tab, choose **News bulletin**, then **Run workflow** to test it. The first run sends stories from the last hour, if there are any.

## Good to know

- **National News Agency stories are headlines only.** NNA's website blocks GitHub's servers, so its headlines come from the public Telegram channel [@NNA_Leb](https://t.me/s/NNA_Leb), which copies NNA's posts on X. That channel is run by a third party, not by NNA, and could stop at any time. Elnashra stories are read with up to 700 characters of the article.
- **The same event can appear twice.** Both sites often cover the same news, and the bulletin doesn't remove these repeats.
- **A red run on the Actions tab means a site failed.** The other site's stories are still sent, and the failed site is tried again on the next run.
- **Keep the repository public.** Actions minutes are free for public repositories. A private one has a monthly limit that this schedule can use up.
- **Runs can start late.** GitHub may delay scheduled runs, so a bulletin can arrive late, but the story is still sent.
- **Schedules can pause.** GitHub turns off schedules in repositories with no activity for 60 days and emails a warning. Turn the workflow back on from the **Actions** tab.
- **The bot adds commits.** After each bulletin it updates `state.json` in the repository. Run `git pull` before pushing your own changes.

## Run it on your own computer

```bash
pip install -r requirements.txt
cp config.example.json config.json   # then add your token and chat ID
python news_bulletin.py --no-send --minutes 60   # build an MP3 of the last hour, without sending
python news_bulletin.py                          # send stories that have not been sent yet
```

Don't run it on a computer and on GitHub at the same time, or stories will be sent twice.

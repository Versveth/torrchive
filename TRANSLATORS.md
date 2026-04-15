# Translating Torrchive

Torrchive uses Python's standard `gettext` system for translations.  
All translation files live in the `locales/` directory.

---

## Adding a new language

1. **Create the directory structure:**
   ```
   locales/
     YOUR_LANG_CODE/
       LC_MESSAGES/
         torrchive.po
   ```
   Use standard locale codes: `de` for German, `es` for Spanish, `it` for Italian, etc.

2. **Copy the English template:**
   ```bash
   cp locales/en/LC_MESSAGES/torrchive.po locales/de/LC_MESSAGES/torrchive.po
   ```

3. **Edit the `.po` file:**
   - Update the `Language:` header
   - Translate each `msgstr` value (leave `msgid` untouched)
   - Leave `msgstr ""` empty if you want to fall back to French for that string

   Example:
   ```po
   msgid "Scanning {} ..."
   msgstr "Scanne {} ..."
   ```

4. **The `.mo` file is compiled automatically** on first run — no manual step needed.

5. **Set your language in `config.yaml`:**
   ```yaml
   language: de
   ```

6. **Submit a PR** with your `.po` file. Include the language name in the PR title.

---

## Updating an existing translation

If new strings were added to Torrchive, they will appear in the English `.po` file with empty `msgstr ""`. Copy them into your language's `.po` file and translate.

---

## Notes

- `{}` placeholders must be preserved exactly as-is in `msgstr`
- The `.mo` binary file is compiled automatically — do not commit it
- French (`fr`) is the default language and the reference translation
- If a string is missing from your translation, it falls back to French

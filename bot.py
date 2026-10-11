name: Tezkor OAK e'lonlar

on:
  schedule:
    - cron: "*/15 * * * *"   # har 15 daqiqada
  workflow_dispatch:          # qo'lda ishga tushirish tugmasi
    inputs:
      ids:
        description: "Qo'lda yuborish: e'lon raqamlari (masalan 32715 yoki 32715,32716). Bo'sh qoldirsangiz — barcha yangi e'lonlar tekshiriladi."
        required: false
        default: ""

permissions:
  contents: write

concurrency:
  group: oak-bot
  cancel-in-progress: false

jobs:
  post:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0              # git tarixi kerak (o'tkazib yuborilgan e'lonlarni aniqlash uchun)
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - name: Yangi e'lonlarni yuborish
        run: python bot.py
        env:
          BOT_TOKEN: ${{ secrets.BOT_TOKEN }}
          CHANNEL_ID: ${{ secrets.CHANNEL_ID }}
          CHANNEL_SIGNATURE: ${{ vars.CHANNEL_SIGNATURE }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          FORCE_IDS: ${{ github.event.inputs.ids }}
      - name: Holatni saqlash
        if: always()
        run: |
          git config user.name "oak-bot"
          git config user.email "oak-bot@users.noreply.github.com"
          git add state.json || true
          if ! git diff --cached --quiet; then
            git commit -m "state yangilandi"
            for i in 1 2 3; do
              git pull --rebase origin main && git push && break
              sleep 5
            done
          fi

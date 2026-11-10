# Card tests (Playwright)

Headless browser tests for `bticino-c100x-card.js` with a mocked `hass`
(no Home Assistant, no camera, no SIP). `harness.html` mocks `ha-card`,
`ha-icon` and HA's `ha-camera-stream > ha-web-rtc-player > <video>` chain.
`?ios=1` emulates the iOS rule that unmuted playback needs `play()` inside a
user gesture on the same element.

These tests are not part of GitHub Actions CI (browser/OS deps). Run them
locally when changing the Lovelace card.

```bash
cd tests/card
npm install && npx playwright install chromium webkit
npm test                 # Chromium (fake mic device)
npm run test:webkit      # WebKit; mic steps fail: no fake capture device
SCREENSHOT_DIR=./shots npm test
```

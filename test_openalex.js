const puppeteer = require('puppeteer');
(async () => {
  const browser = await puppeteer.launch();
  const page = await browser.newPage();
  page.on('console', msg => console.log('PAGE LOG:', msg.text()));
  page.on('pageerror', error => console.log('PAGE ERROR:', error.message));
  page.on('response', response => {
    if(!response.ok()) console.log('HTTP ERROR:', response.url(), response.status());
  });
  await page.goto('http://127.0.0.1:8080', {waitUntil: 'networkidle0'});
  await browser.close();
})();

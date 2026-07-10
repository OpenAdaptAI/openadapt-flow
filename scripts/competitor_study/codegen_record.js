// Record the canonical MockMed task with Playwright codegen's own recorder.
//
// `playwright codegen` requires a human at the keyboard; this script drives
// the SAME recorder (`context._enableRecorder`, the internal API the codegen
// CLI uses) with raw mouse/keyboard input dispatched at element coordinates
// -- human-shaped trusted events, so the recorder emits exactly the locators
// it would emit for a person performing the task. The emitted Python script
// is saved UNEDITED and replayed as the no-AI incumbent's floor.
//
// Usage: node codegen_record.js <playwright-package-dir> <output-file> <note>
const [, , pkgDir, outputFile, note] = process.argv;
const pw = require(pkgDir);

async function rawClick(page, selector) {
  const box = await page.locator(selector).boundingBox();
  if (!box) throw new Error('no box for ' + selector);
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await page.waitForTimeout(500);
}

(async () => {
  const browser = await pw.chromium.launch({ headless: false });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
  });
  await context._enableRecorder({
    language: 'python',
    mode: 'recording',
    outputFile: outputFile,
  });
  const page = await context.newPage();
  await page.goto('http://127.0.0.1:8765/');
  await page.waitForSelector('#username');
  await page.waitForTimeout(500);

  await rawClick(page, '#username');
  await page.waitForTimeout(1200); // let the recorder fully attach
  await page.keyboard.type('nurse.demo', { delay: 50 });
  await rawClick(page, '#password');
  await page.keyboard.type('mockmed-demo-pass', { delay: 50 });
  await rawClick(page, '#signin');
  await page.waitForSelector('#tasks-table');
  await page.waitForTimeout(500);
  await rawClick(page, '#open-p1'); // FIRST referral row (Jane Sample)
  await page.waitForSelector('#new-encounter');
  await rawClick(page, '#new-encounter');
  await page.waitForSelector('#type-triage');
  await rawClick(page, '#type-triage');
  await rawClick(page, '#note');
  await page.keyboard.type(note, { delay: 30 });
  await rawClick(page, '#save-encounter');
  await page.waitForSelector('#saved-banner');
  await page.waitForTimeout(1500);
  await context.close();
  await browser.close();
})();

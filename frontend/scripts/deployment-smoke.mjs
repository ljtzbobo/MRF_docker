import { chromium } from 'playwright-core'
import { mkdir } from 'node:fs/promises'

const browser = await chromium.launch({
  executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  headless: true,
})
const base = process.env.MRF_FRONTEND_URL || 'http://127.0.0.1:5174'
await mkdir('../outputs', { recursive: true })

try {
  for (const viewport of [{ name: 'desktop', width: 1440, height: 900 },
    { name: 'mobile', width: 390, height: 844 }]) {
    const page = await browser.newPage({ viewport })
    const errors = []
    page.on('pageerror', (error) => errors.push(error.message))
    await page.goto(base, { waitUntil: 'networkidle' })
    await page.getByText('已匹配', { exact: true }).waitFor()
    const submit = page.getByRole('button', { name: '提交执行' })
    if (await submit.isEnabled()) throw new Error('初始样例不应允许部署')
    await page.getByRole('button', { name: '载入接收链样例' }).click()
    await page.getByText('接收链演示帧.json').waitFor()
    await page.getByText('FEASIBLE_ESTIMATE').waitFor()
    if (!await submit.isEnabled()) throw new Error('可行接收链未启用提交')
    await submit.click()
    await page.getByText('COMPLETED', { exact: true }).waitFor({ timeout: 60000 })
    await page.getByText('2 帧完成').waitFor()
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2)
    if (errors.length || overflow) throw new Error(JSON.stringify({ viewport: viewport.name, errors, overflow }))
    await page.screenshot({ path: `../outputs/deployment-${viewport.name}.png`, fullPage: true })
    console.log(`${viewport.name}: execution complete, errors=0, overflow=false`)
    await page.close()
  }
} finally {
  await browser.close()
}

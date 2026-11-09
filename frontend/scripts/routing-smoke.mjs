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
    await page.goto(`${base}/resources`, { waitUntil: 'networkidle' })
    await page.getByRole('heading', { name: '容器资源' }).waitFor()
    await page.getByText('物理容量与预留').waitFor()
    await page.reload({ waitUntil: 'networkidle' })
    await page.getByRole('heading', { name: '容器资源' }).waitFor()
    await page.getByRole('link', { name: '任务拆解' }).click()
    await page.getByText('已匹配', { exact: true }).waitFor()
    if (!page.url().endsWith('/tasks')) throw new Error('任务路由没有更新地址')
    await page.getByText('资源 R', { exact: true }).click()
    await page.getByText('资源需求', { exact: true }).waitFor()
    await page.getByText('未估算', { exact: true }).first().waitFor()
    const serialized = await page.locator('.representation-parts').getByText('{"cpu_millicores"').count()
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2)
    if (serialized || overflow || errors.length) {
      throw new Error(JSON.stringify({ viewport: viewport.name, serialized, overflow, errors }))
    }
    await page.screenshot({ path: `../outputs/routing-resource-${viewport.name}.png`, fullPage: true })
    await page.goBack({ waitUntil: 'networkidle' })
    await page.getByRole('heading', { name: '容器资源' }).waitFor()
    console.log(`${viewport.name}: direct route, navigation, back, resource details passed`)
    await page.close()
  }
} finally {
  await browser.close()
}

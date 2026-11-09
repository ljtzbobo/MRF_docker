import { chromium } from 'playwright-core'
import { readFile } from 'node:fs/promises'

const browser = await chromium.launch({
  executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  headless: true,
})
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
const errors = []
page.on('pageerror', (error) => errors.push(error.message))
const input = page.locator('input[type=file]').first()

async function upload(name, buffer) {
  await input.setInputFiles({ name, mimeType: 'application/json', buffer })
}

try {
  const base = process.env.MRF_FRONTEND_URL || 'http://127.0.0.1:5173'
  await page.goto(`${base}/tasks`, { waitUntil: 'networkidle' })

  const spectrum = await readFile('../examples/BusinessRequests/External/07_spectrum_analysis.json')
  if ('G' in JSON.parse(spectrum.toString())) throw new Error('原始样例预先包含功能图')
  await upload('spectrum.json', spectrum)
  await page.getByText('spectrum.json').waitFor()
  await page.locator('.task-meta').getByText('spectrum_analysis', { exact: true }).waitFor()
  await page.getByText('已匹配', { exact: true }).waitFor()
  if (await page.locator('.flow-node').count() !== 4) {
    throw new Error('原始频谱请求没有拆解出四个业务功能节点')
  }

  const collection = await readFile('../examples/BusinessRequests/External/all_tasks.json')
  await upload('all_tasks.json', collection)
  await page.getByLabel('选择仿真任务').waitFor()
  await page.getByLabel('选择仿真任务').selectOption('RF-COM-DATALINK-001')
  await page.locator('.task-meta').getByText('data_link_transfer', { exact: true }).waitFor()
  await page.getByText('待细化', { exact: true }).waitFor()
  await page.getByRole('tab', { name: '业务功能 G' }).click()
  if (await page.locator('.flow-node').count() !== 3) {
    throw new Error('数据链业务请求没有拆解出三个业务功能节点')
  }

  await upload('tasks-array.json', Buffer.from(JSON.stringify(JSON.parse(collection.toString()).tasks)))
  await page.getByLabel('选择仿真任务').selectOption('RF-COM-VOICE-001')
  await page.locator('.task-meta').getByText('voice_communication', { exact: true }).waitFor()

  const receive = await readFile('public/samples/data-link-rx-task.json')
  await upload('receive.json', receive)
  await page.getByText('receive.json').waitFor()
  await page.getByText('已匹配', { exact: true }).waitFor()

  const normalized = await readFile('public/samples/data-link-rx-representation.json')
  await upload('normalized.json', normalized)
  await page.getByText('normalized.json').waitFor()
  await page.getByText('已匹配', { exact: true }).waitFor()

  await page.setViewportSize({ width: 390, height: 844 })
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2)) {
    throw new Error('窄屏页面出现横向溢出')
  }

  if (errors.length) throw new Error(`页面异常: ${errors.join('; ')}`)
  console.log('原始任务拆解、两种合集、接收链匹配、表征导入及窄屏检查均通过')
} finally {
  await browser.close()
}

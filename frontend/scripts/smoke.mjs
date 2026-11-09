import { chromium } from 'playwright-core'
import { mkdir } from 'node:fs/promises'
import { readFile } from 'node:fs/promises'

const browser = await chromium.launch({
  executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  headless: true,
})
const base = 'http://127.0.0.1:5173'
const output = '../outputs'
await mkdir(output, { recursive: true })

try {
  for (const viewport of [{ name: 'desktop', width: 1440, height: 900 },
    { name: 'mobile', width: 390, height: 844 }]) {
    const page = await browser.newPage({ viewport: { width: viewport.width, height: viewport.height } })
    const errors = []
    page.on('pageerror', (error) => errors.push(error.message))
    await page.goto(base, { waitUntil: 'networkidle' })
    await page.getByText('已匹配', { exact: true }).waitFor()
    const upload = await readFile('../examples/BusinessRequests/External/07_spectrum_analysis.json')
    await page.locator('input[type=file]').setInputFiles({ name: 'uploaded-task.json', mimeType: 'application/json', buffer: upload })
    await page.getByText('uploaded-task.json').waitFor()
    await page.getByText('已匹配', { exact: true }).waitFor()
    await page.getByText('语义 S').waitFor()
    await page.getByText('语义 S').click()
    await page.getByText('task_type', { exact: true }).waitFor()
    const business = await page.locator('.flow-node').count()
    await page.getByRole('tab', { name: '规格 DAG' }).click()
    const specification = await page.locator('.flow-node').count()
    await page.getByRole('tab', { name: '组件执行图' }).click()
    const component = await page.locator('.flow-node').count()
    await page.locator('.flow-node').first().click()
    await page.getByText('节点详情').waitFor()
    await page.screenshot({ path: `${output}/workbench-${viewport.name}.png`, fullPage: true })
    await page.getByRole('button', { name: '容器资源' }).click()
    await page.getByText('尚未配置物理部署').waitFor()
    await page.screenshot({ path: `${output}/resources-${viewport.name}.png`, fullPage: true })
    await page.route('**/api/v1/deployments/resources', (route) => route.fulfill({ json: {
      configured: true,
      nodes: [{ node_id: 'local-docker', hardware_id: 'test-node',
        capacity: { cpu_millicores: 4000, memory_bytes: 8589934592, bandwidth_bytes_per_s: 104857600, dsp_units: 0, fpga_units: 0 },
        reserved: { cpu_millicores: 800, memory_bytes: 1073741824, bandwidth_bytes_per_s: 20971520, dsp_units: 0, fpga_units: 0 } }],
      active_tasks: ['RF-COM-DATALINK-001'],
      containers: [{ container_unit_id: 'RF-COM-DATALINK-001:rx-02:ofdm_rx', cid: 'C15', processing_node_id: 'local-docker',
        state: 'ACTIVE', task_id: 'RF-COM-DATALINK-001', actual: { status: 'RUNNING', cpu_percent: '3.5%', memory_usage: '40MiB / 128MiB', network_io: '2kB / 4kB' } }],
    } }))
    await page.getByRole('button', { name: '刷新资源' }).click()
    await page.locator('.resource-chart:not(.resource-trend) canvas').waitFor()
    await page.locator('.resource-trend canvas').waitFor()
    await page.getByText('40MiB / 128MiB').waitFor()
    await page.waitForTimeout(600)
    await page.screenshot({ path: `${output}/resources-active-${viewport.name}.png`, fullPage: true })
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2)
    if (errors.length || overflow || !business || !specification || !component) {
      throw new Error(JSON.stringify({ viewport: viewport.name, errors, overflow, business, specification, component }))
    }
    console.log(`${viewport.name}: business=${business}, specification=${specification}, component=${component}, errors=0, overflow=false`)
    await page.close()
  }
} finally {
  await browser.close()
}

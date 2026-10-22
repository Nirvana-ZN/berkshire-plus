const JOB_STATUS: Readonly<Record<string, string>> = {
  queued: '排队',
  running: '进行中',
  done: '完成',
  failed: '失败',
}

const DISCOVERY_STATUS: Readonly<Record<string, string>> = {
  queued: '排队中',
  running: '进行中',
  waiting_confirmation: '等待确认',
  completed: '已完成',
  partially_completed: '部分完成',
  failed: '失败',
  abandoned: '已放弃',
}

export const WORKBENCH_REFRESH_MS = 3_000
export const WORKBENCH_FETCH_TIMEOUT_MS = 2_500
export const WORKBENCH_PATHS = [
  '/api/health',
  '/api/jobs',
  '/api/watchlist',
  '/api/reviews/latest',
  '/api/sentiment',
  '/api/aftermath',
  '/api/discoveries/current',
] as const

export function discoveryStatusText(status: string): string {
  return DISCOVERY_STATUS[status] || status
}

export function jobStatusText(type: string, status: string): string {
  return type === 'discovery'
    ? discoveryStatusText(status)
    : JOB_STATUS[status] || status
}

function escapeHtml(raw: string): string {
  return raw
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function inlineMd(escaped: string): string {
  return escaped
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
}

export function renderMarkdownHtml(source: string | null | undefined): string {
  if (!source) return ''
  const lines = source.replace(/\r\n/g, '\n').split('\n')
  const out: string[] = []
  let inUl = false
  let inOl = false
  let inTable = false
  let tableHeadDone = false

  function closeLists() {
    if (inUl) {
      out.push('</ul>')
      inUl = false
    }
    if (inOl) {
      out.push('</ol>')
      inOl = false
    }
  }

  function closeTable() {
    if (inTable) {
      out.push('</tbody></table>')
      inTable = false
      tableHeadDone = false
    }
  }

  for (let i = 0; i < lines.length; i += 1) {
    const raw = (lines[i] ?? '').trim()
    if (!raw) {
      closeLists()
      closeTable()
      continue
    }
    if (raw.startsWith('|') && raw.endsWith('|') && raw.length > 2) {
      closeLists()
      const cells = raw
        .slice(1, -1)
        .split('|')
        .map((c) => inlineMd(escapeHtml(c.trim())))
      if (cells.every((c) => /^[-:\s]+$/.test(c))) {
        continue
      }
      if (!inTable) {
        out.push('<table class="md-table"><thead><tr>')
        for (const c of cells) out.push(`<th>${c}</th>`)
        out.push('</tr></thead><tbody>')
        inTable = true
        tableHeadDone = true
      } else if (tableHeadDone) {
        out.push('<tr>')
        for (const c of cells) out.push(`<td>${c}</td>`)
        out.push('</tr>')
      }
      continue
    }
    closeTable()

    if (/^---+$/.test(raw)) {
      closeLists()
      out.push('<hr />')
      continue
    }
    const hMatch = /^(#{1,4})\s+(.+)$/.exec(raw)
    if (hMatch) {
      closeLists()
      const level = Math.min(4, Math.max(2, hMatch[1]!.length + 1))
      out.push(`<h${level}>${inlineMd(escapeHtml(hMatch[2]!))}</h${level}>`)
      continue
    }
    const ulMatch = /^[-*]\s+(.+)$/.exec(raw)
    if (ulMatch) {
      if (inOl) {
        out.push('</ol>')
        inOl = false
      }
      if (!inUl) {
        out.push('<ul>')
        inUl = true
      }
      out.push(`<li>${inlineMd(escapeHtml(ulMatch[1]!))}</li>`)
      continue
    }
    const olMatch = /^\d+\.\s+(.+)$/.exec(raw)
    if (olMatch) {
      if (inUl) {
        out.push('</ul>')
        inUl = false
      }
      if (!inOl) {
        out.push('<ol>')
        inOl = true
      }
      out.push(`<li>${inlineMd(escapeHtml(olMatch[1]!))}</li>`)
      continue
    }
    closeLists()
    out.push(`<p>${inlineMd(escapeHtml(raw))}</p>`)
  }
  closeLists()
  closeTable()
  return out.join('\n')
}

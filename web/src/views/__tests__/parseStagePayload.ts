import { parseStageResponse, renderMetricValue } from '../discoveryViewModel.ts'

let input = ''
process.stdin.setEncoding('utf8')
for await (const chunk of process.stdin) input += chunk

const parsed = parseStageResponse(JSON.parse(input))
if (parsed === null) {
  process.stderr.write('stage payload rejected')
  process.exitCode = 2
} else {
  const row = [...parsed.pass, ...parsed.drop, ...parsed.insufficient][0]
  process.stdout.write(JSON.stringify({
    code: row?.code ?? null,
    risk_flags: row?.metrics.risk_flags === undefined
      ? null
      : renderMetricValue(row.metrics.risk_flags),
  }))
}

import type { HistoryItem } from './types'
import { record, text } from './perception'
import { Files } from './history-files'

export function PerceptionRecord({ item }: { item: HistoryItem }) {
  const payload = record(item.raw?.payload)
  const visual = record(payload.value).visual_observations
  const descriptions = new Map(
    (Array.isArray(visual) ? visual : []).map((value) => {
      const frame = record(value)
      return [frame.frame_index, text(frame.text)] as const
    }),
  )
  const files = item.files
  return (
    <>
      {item.raw ? (
        <details className="stage-disclosure">
          <summary>原始 JSON</summary>
          <pre className="perception-json">{JSON.stringify(item.raw, null, 2)}</pre>
        </details>
      ) : (
        <p>{item.text || '原始 JSON 未记录。'}</p>
      )}
      {files.length ? (
        <details className="stage-disclosure">
          <summary>附件（{files.length}）</summary>
          {payload.image_recognition === 'pending' && <small role="status">正在识别截图</small>}
          {payload.image_recognition === 'retrying' && (
            <small role="status">部分截图等待重试</small>
          )}
          {payload.image_recognition === 'awaiting_model' && (
            <small role="status">截图已保存，等待配置视觉模型</small>
          )}
          {files.map((file) => {
            const index = /:frame:(\d+)$/.exec(file.id)
            const description = index && descriptions.get(Number(index[1]))
            return (
              <div key={file.id}>
                <Files files={[file]} />
                {description && <p>{description}</p>}
              </div>
            )
          })}
        </details>
      ) : (
        <p className="stage-note">无附件</p>
      )}
    </>
  )
}

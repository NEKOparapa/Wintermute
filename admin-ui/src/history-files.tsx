import { FileDown } from 'lucide-react'
import { fileUrl } from './api'
import type { HistoryFile } from './types'

export function Files({ files }: { files: HistoryFile[] }) {
  if (!files.length) return null
  return (
    <ul className="history-files" aria-label="附件">
      {files.map((file) => {
        const url = fileUrl(file.id)
        const image =
          file.content_type === 'image/jpeg' || (!file.content_type && /\.jpe?g$/i.test(file.name))
        return (
          <li key={file.id} className={image ? 'history-screenshot' : undefined}>
            {image && (
              <figure>
                <img loading="lazy" src={url} alt={file.window_title || file.name} />
              </figure>
            )}
            <a className="history-file" href={url} download={file.name}>
              <FileDown size={14} aria-hidden="true" />
              {file.name}
            </a>
          </li>
        )
      })}
    </ul>
  )
}

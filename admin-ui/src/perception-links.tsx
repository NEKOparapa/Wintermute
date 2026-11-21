import { traceMembers } from './api'
import { useCallback } from 'react'
import type { SummaryRoundRef, TraceMember, TracePage } from './types'
import { usePages } from './use-pages'
import { Navigation, OriginalDisclosure } from './compression-browser'

export type TraceNavigation = {
  openMember: (member: TraceMember) => void
  openSummary: (round: SummaryRoundRef) => void
}

export function PerceptionLinks({
  target,
  openMember,
  openSummary,
}: Pick<TraceNavigation, 'openMember' | 'openSummary'> & {
  target: string
}) {
  const fetch = useCallback((cursor?: string) => traceMembers(target, cursor), [target])
  const result = usePages<TraceMember, TracePage>(fetch)
  const page = result.page
  const prior = page?.round?.previous_summary
  return (
    <section className="trace-group">
      {page && (
        <>
          {page.round && (
            <>
              <h4>AI 摘要原文</h4>
              <pre className="stage-text">{page.round.summary}</pre>
              {page.round.archived_text_only && (
                <p className="stage-note">这条旧摘要仅有当时保存的文本。</p>
              )}
              <details className="stage-disclosure">
                <summary>查看本轮完整输入</summary>
                <pre className="stage-text">{page.round.input}</pre>
              </details>
            </>
          )}
          <h4>引用记录</h4>
          <ul className="trace-members">
            {prior && (
              <li>
                <button className="history-inline" onClick={() => openSummary(prior)}>
                  上一轮 AI 摘要 ·{' '}
                  {new Date(prior.occurred_at).toLocaleString('zh-CN', {
                    hour12: false,
                  })}{' '}
                  ↗
                </button>
              </li>
            )}
            {result.items.map((member) => {
              const label = [
                new Date(member.occurred_at).toLocaleString('zh-CN', {
                  hour12: false,
                }),
                member.device_id,
                member.title,
              ]
                .filter(Boolean)
                .join(' · ')
              return (
                <li key={member.id}>
                  {member.event_id ? (
                    <button className="history-inline" onClick={() => openMember(member)}>
                      {label} ↗
                    </button>
                  ) : (
                    <>
                      <span>{label}</span>
                      <OriginalDisclosure layer="slice" id={member.id} activity />
                    </>
                  )}
                </li>
              )
            })}
          </ul>
          {!result.items.length && !prior && (
            <p className="stage-note">本轮没有可定位的引用记录。</p>
          )}
        </>
      )}
      <Navigation {...result} more="更多关联记录" />
    </section>
  )
}

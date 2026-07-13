import React from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// Renders ticket/agent markdown (headings, lists, tables, fenced code) as
// readable HTML. Tailwind's `prose` handles typography; we tighten it for the
// compact ticket drawer and give code/tables sensible styling.
export default function Markdown({ children }) {
  return (
    <div className="prose prose-sm max-w-none prose-slate
      prose-headings:font-semibold prose-headings:text-slate-800
      prose-h1:text-base prose-h2:text-sm prose-h3:text-sm
      prose-p:my-1.5 prose-ul:my-1.5 prose-ol:my-1.5 prose-li:my-0.5
      prose-pre:bg-slate-800 prose-pre:text-slate-100 prose-pre:text-xs prose-pre:my-2
      prose-code:text-indigo-700 prose-code:bg-indigo-50 prose-code:px-1 prose-code:rounded
      prose-code:before:content-none prose-code:after:content-none
      prose-table:text-xs prose-th:bg-slate-100 prose-td:border prose-th:border
      prose-a:text-indigo-600">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children || ''}</ReactMarkdown>
    </div>
  )
}

import { useCallback, useEffect, useRef, useState } from 'react'
import { t } from '@/app/i18n';
import { useMessagePop } from '@/shared/components/MessagePop';
import { UploadedFileItem } from '@/shared/components/ChatInputControl'
import { deleteSessionFile, fetchSessionFiles, uploadSessionFile, type SessionFile } from '../sessionFiles'

/**
 * The conversation's file library.
 *
 * Replaces a hook that posted to `/api/files/upload` -- an endpoint that never
 * existed here -- and kept the result in component state that a model switch
 * wiped. Files belong to the conversation, so they are reloaded when the
 * conversation changes and survive a failed turn.
 */

/** Mirrors the server's allowlist; the server still has the final say. */
export const ACCEPTED_EXTENSIONS = [
  '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
  '.txt', '.md', '.csv', '.tsv', '.json', '.jsonl', '.yaml', '.yml', '.xml', '.log', '.rtf',
  '.html', '.htm', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tif', '.tiff', '.zip',
]

export const UPLOAD_ACCEPT = ACCEPTED_EXTENSIONS.join(',')

const MAX_LABEL_MB = 25

const toItem = (row: SessionFile): UploadedFileItem => ({
  fileId: row.fileId, serverFileId: row.fileId, name: row.name,
  mimeType: row.mimeType, size: row.size,
})

interface UseSessionFilesOptions {
  sessionId: string;
  /** False while the conversation cannot run tools, which is what reads them. */
  enabled: boolean;
  isLoading: boolean;
  /**
   * Called when a file arrives before the conversation exists. The id is
   * minted by the first send, but a reader attaches the file first, so the
   * upload has to be able to start the conversation instead of refusing.
   */
  ensureSession?: () => string;
}

export const useSessionFiles = ({ sessionId, enabled, isLoading, ensureSession }: UseSessionFilesOptions) => {
  const messagePop = useMessagePop()
  const uploadController = useRef<AbortController | null>(null)
  const [isUploadingFile, setIsUploadingFile] = useState(false)
  const [uploadedFiles, setUploadedFiles] = useState<UploadedFileItem[]>([])

  const reload = useCallback(async (signal?: AbortSignal, target?: string) => {
    const id = target ?? sessionId
    if (!enabled || !id) { setUploadedFiles([]); return }
    try {
      const rows = await fetchSessionFiles(id, signal)
      if (!signal?.aborted) setUploadedFiles(rows.map(toItem))
    } catch (error) {
      // A list that cannot load is not worth a toast: the composer still works,
      // and the next turn reports the real failure if the session is gone.
      if (!signal?.aborted) setUploadedFiles([])
      void error
    }
  }, [enabled, sessionId])

  useEffect(() => {
    const controller = new AbortController()
    setUploadedFiles([])
    void reload(controller.signal)
    return () => controller.abort()
  }, [reload])

  useEffect(() => () => { uploadController.current?.abort() }, [])

  const onUploadFile = async (file: File) => {
    if (!enabled || isUploadingFile || isLoading) return
    const dot = file.name.lastIndexOf('.')
    const suffix = dot >= 0 ? file.name.slice(dot).toLowerCase() : ''
    if (!ACCEPTED_EXTENSIONS.includes(suffix)) {
      messagePop.error(t('input.uploadTypeError', { name: file.name }))
      return
    }
    if (file.size > MAX_LABEL_MB * 1024 * 1024) {
      messagePop.error(t('input.uploadTooLarge', { size: MAX_LABEL_MB }))
      return
    }
    const target = sessionId || ensureSession?.() || ''
    if (!target) {
      messagePop.error(t('input.uploadError'))
      return
    }
    const controller = new AbortController()
    uploadController.current = controller
    setIsUploadingFile(true)
    try {
      const row = await uploadSessionFile(target, file, controller.signal)
      if (!controller.signal.aborted) setUploadedFiles(previous => [...previous, toItem(row)])
    } catch (error) {
      if (!controller.signal.aborted) {
        messagePop.error(error instanceof Error ? error.message : t('input.uploadError'))
      }
    } finally {
      if (uploadController.current === controller) {
        setIsUploadingFile(false)
        uploadController.current = null
      }
    }
  }

  const onRemoveUploadedFile = (uploadedFileId: string) => {
    setUploadedFiles(previous => previous.filter(file => file.fileId !== uploadedFileId))
    // Best effort: the list already forgot it, and a failure leaves the file in
    // the conversation rather than losing the reader's local state twice.
    void deleteSessionFile(sessionId || ensureSession?.() || '', uploadedFileId)
      .then(() => reload())
      .catch(() => { void 0 })
  }

  return {
    uploadedFiles,
    isUploadingFile,
    fileIds: uploadedFiles.map(file => file.fileId),
    onUploadFile,
    onRemoveUploadedFile,
    reload,
  }
}

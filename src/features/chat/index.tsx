import { languageHeaders } from '@/app/i18n';
import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import ChatHeaderOperate from '@/shared/components/ChatHeaderOperate';
import MessageItem from '@/shared/components/MessageItem';
import Sidebar from '@/shared/components/Sidebar';
import Share from '@/shared/components/Share';
import Dialog from '@/shared/components/Dialog';
import ChatInputControl from '@/shared/components/ChatInputControl';
import ArrowDownIcon from '@/shared/assets/arrowDown.svg?react';
import { useChat, useChatDispatch } from '@/app/providers/ChatContext';
import { useLanguage } from '@/app/providers/LanguageContext';
import { newChat, storageMessages, ensureAgentTurnId, removeLastAssistantMessage, Message, getSelectId, getMessageByCovId } from '@/shared/utils/localMessages'
import { MODEL_STORAGE_KEY, SHOW_SANDBOX_AND_SKILLS_UI } from './constants';
import { SSEData } from './types';
import { useChatModels } from './hooks/useChatModels';
import { useFileUpload } from './hooks/useFileUpload';
import { useStickToBottom } from './hooks/useStickToBottom';
import { streamAgents, stopSession } from '@/features/agents/api';
import { ApprovalCard, type Approval } from '@/features/agents/components/ApprovalCard';
import type { MessageApproval } from '@/shared/utils/localMessages';
import { CapabilitiesPanel, type CapabilitySelection } from '@/features/agents/components/CapabilitiesPanel';
import type { AgentArtifact } from '@/features/agents/types';
import { ArtifactPreviewPanel } from '@/features/agents/components/ArtifactPreviewPanel';
import {
  buildContextMessages,
  cloneMessage,
  isImageAttachment,
  toRequestMessage
} from './utils';

import { WelcomeMascot } from './components/WelcomeMascot';
import './chat.css';

/**
 * At or below 1100px the artifact preview panel covers the whole message area
 * (chat.css marks .messages-content as visibility:hidden), so auto-opening it
 * while an artifact streams in would hide the entire conversation. Manual
 * opens from artifact cards stay allowed at any width.
 */
const canAutoOpenPreview = () => window.matchMedia('(min-width: 1101px)').matches;

const ChatAI: React.FC = () => {
  const controllerRef = useRef<AbortController | null>(null);
  const chatApiUrl = ((import.meta as any).env.VITE_CHAT_BASE_URL || '/api/chat/completions') as string
  const [inputText, setInputText] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [sandbox, setSandbox] = useState(true);
  const [capabilities, setCapabilities] = useState<CapabilitySelection>({ skills: [], mcp_servers: [] });
  const [loadedSkills, setLoadedSkills] = useState<string[]>([]);
  const [skillCatalogVersion, setSkillCatalogVersion] = useState(0);
  const [pendingSkillSession, setPendingSkillSession] = useState<CapabilitySelection | null>(null);
  const [approvals, setApprovals] = useState<(Approval & { sessionId: string })[]>([]);
  const [isAgentMode, setIsAgentMode] = useState(() => localStorage.getItem('chat.agentMode') === 'true');
  const [liveFiles, setLiveFiles] = useState<{ sessionId: string; files: AgentArtifact[] }>({ sessionId: '', files: [] });
  const [previewSelection, setPreviewSelection] = useState<{ sessionId: string; artifact: AgentArtifact } | null>(null);
  const setPreviewArtifact = useCallback((artifact: AgentArtifact) => {
    setPreviewSelection({ sessionId: getSelectId() || '', artifact });
  }, []);
  const [isShowScrollBtn, setIsShowScrollBtn] = useState(false)
  const [isShowShare, setIsShowShare] = useState(false)
  const [shareTargetElement, setShareTargetElement] = useState<HTMLElement | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const { containerRef: messagesRef, follow: followStream, scrollToBottom: scrollMessagesToBottom } = useStickToBottom();
  const streamFrameRef = useRef<number | null>(null)
  const streamTimerRef = useRef<number | null>(null)
  const streamLastPaintRef = useRef(0)
  const streamPendingMessageRef = useRef<Message | null>(null)
  const { t } = useLanguage()
  const { messages } = useChat()
  const activeSessionId = getSelectId();
  // The runtime measures the live transcript; keep it per session so switching
  // conversations never shows another session's headroom.
  const [contextUsage, setContextUsage] = useState<{ sessionId: string; tokens: number; window: number } | null>(null);
  // Starting a new conversation clears the messages but keeps selectCovId pointing
  // at the previous one until the first message is sent, so the session alone is
  // not proof that this view owns the reading: require actual history too.
  const activeContextUsage = contextUsage?.sessionId === activeSessionId && messages.length > 0
    ? { tokens: contextUsage.tokens, window: contextUsage.window } : null;
  const sessionApprovals = approvals.filter(approval => approval.sessionId === activeSessionId);
  const pendingApprovals = sessionApprovals.filter(approval => approval.status === 'pending');
  const pendingApproval = pendingApprovals[0];
  const composerRef = useRef<HTMLDivElement>(null);
  const approvalFocusSession = useRef<string | null>(null);
  const dispatch = useChatDispatch()
  const previewArtifact = !pendingApproval && previewSelection?.sessionId === getSelectId() && messages.length ? previewSelection.artifact : null
  const previewArtifacts = [...messages.flatMap(message => message.artifacts || []), ...(liveFiles.sessionId === getSelectId() ? liveFiles.files : [])]
  if (previewArtifact && !previewArtifacts.some(file => file.fileId === previewArtifact.fileId)) previewArtifacts.push(previewArtifact)
  const isNewConversation = messages.length === 0 && localStorage.getItem('isNewCov') === 'true'
  const lastMessage = messages[messages.length - 1]
  // The assistant bubble renders its own live state (agent activity line or caret),
  // so the extra dot row would only add a block that pops in and out mid-stream.
  const showTypingIndicator = isLoading && !pendingApproval
    && !lastMessage?.agentStatus && !lastMessage?.content && !lastMessage?.reasoning_content
  useEffect(() => {
    if (pendingApproval) approvalFocusSession.current = activeSessionId;
    else if (!isLoading && approvalFocusSession.current === activeSessionId) {
      composerRef.current?.querySelector('textarea')?.focus();
      approvalFocusSession.current = null;
    }
  }, [pendingApproval, activeSessionId, isLoading]);
  useEffect(() => {
    const draft = isNewConversation || !activeSessionId;
    // The sandbox checkbox is hidden, so new conversations always use Docker while existing
    // conversations keep the environment they were created with (switching is refused by the API).
    setSandbox(draft || localStorage.getItem(`chat.sandbox.${activeSessionId}`) === 'true');
    try {
      // With the panel hidden there is no way to change selections, so do not resend them.
      const saved = JSON.parse(draft || !SHOW_SANDBOX_AND_SKILLS_UI ? '{}'
        : localStorage.getItem(`chat.capabilities.${activeSessionId}`) || '{}');
      setCapabilities({ skills: Array.isArray(saved.skills) ? saved.skills : [],
        mcp_servers: Array.isArray(saved.mcp_servers) ? saved.mcp_servers : [] });
      const loaded = JSON.parse(draft ? '[]' : localStorage.getItem(`chat.loadedSkills.${activeSessionId}`) || '[]');
      setLoadedSkills(Array.isArray(loaded) ? loaded : []);
    } catch { setCapabilities({ skills: [], mcp_servers: [] }); setLoadedSkills([]); }
  }, [activeSessionId, isNewConversation]);
  const hasSelectedConversation = Boolean(getSelectId())
  const isWelcomeConversation = messages.length === 0 && (isNewConversation || !hasSelectedConversation)
  const selectedConversation = useMemo(() => {
    if (isNewConversation) return null
    const selectId = getSelectId()
    if (!selectId) return null
    return getMessageByCovId(selectId).curMessage
  }, [messages, isNewConversation])
  const {
    models,
    isModelsLoading,
    isBackendUnavailable,
    setSelectedModelId,
    selectedModel,
    selectedModelId,
    selectedModelName,
    supportsImageUnderstanding,
    modelSupportsThinking,
    supportsFileUpload,
    isThinkingEnabled,
    onToggleThinking
  } = useChatModels({
    chatApiUrl,
    selectedConversation,
    isLoading,
    defaultDescription: t('chat.defaultDescription')
  })
  const isImageModel = selectedModel?.supportsImageGeneration === true;
  const selectCapabilities = (next: CapabilitySelection) => {
    if (next.skills.length && !sandbox && messages.length) {
      setPendingSkillSession(next);
      return;
    }
    if (next.skills.length) setSandbox(true);
    setCapabilities(next);
  };
  const prepareSkillSession = () => {
    if (messages.length) setPendingSkillSession(capabilities);
    else setSandbox(true);
  };
  const confirmSkillSession = () => {
    if (!pendingSkillSession) return;
    newChat({ id: selectedModelId, name: selectedModelName });
    const id = getSelectId();
    localStorage.setItem('isNewCov', 'false');
    localStorage.setItem(`chat.sandbox.${id}`, 'true');
    localStorage.setItem(`chat.capabilities.${id}`, JSON.stringify(pendingSkillSession));
    setSandbox(true);
    setCapabilities(pendingSkillSession);
    setPendingSkillSession(null);
    dispatch({ type: 'clearMessages' });
    dispatch({ type: 'getCovList' });
  };
  const {
    uploadedFiles,
    isUploadingFile,
    setUploadedFiles,
    onUploadFile,
    onRemoveUploadedFile
  } = useFileUpload({
    chatApiUrl,
    selectedModelId,
    supportsFileUpload,
    supportsImageUnderstanding,
    isLoading
  })

  const handleInputChange = (value: string) => {
    setInputText(value);
  }

  // 跟随流式输出的滚动：仅在用户停留底部时贴底，避免每一帧重置平滑滚动
  const scrollToBottom = (behavior: ScrollBehavior = 'auto') => {
    scrollMessagesToBottom(behavior);
  };

  /** Streaming repaints are capped at ~30fps so text growth stays smooth instead of jittery. */
  const STREAM_PAINT_INTERVAL = 32;

  const cancelStreamRender = () => {
    if (streamTimerRef.current !== null) {
      window.clearTimeout(streamTimerRef.current)
      streamTimerRef.current = null
    }
    if (streamFrameRef.current !== null) {
      cancelAnimationFrame(streamFrameRef.current)
      streamFrameRef.current = null
    }
  }

  const paintStream = () => {
    streamFrameRef.current = null
    streamLastPaintRef.current = performance.now()
    const pendingMessage = streamPendingMessageRef.current
    if (!pendingMessage) return
    dispatch({
      type: 'addMessages',
      messages: cloneMessage(pendingMessage)
    } as any)
  }

  const flushStreamRender = () => {
    cancelStreamRender()
    paintStream()
  }

  const scheduleStreamRender = () => {
    if (streamTimerRef.current !== null || streamFrameRef.current !== null) return
    const wait = Math.max(0, STREAM_PAINT_INTERVAL - (performance.now() - streamLastPaintRef.current))
    streamTimerRef.current = window.setTimeout(() => {
      streamTimerRef.current = null
      streamFrameRef.current = requestAnimationFrame(paintStream)
    }, wait)
  }

  useEffect(() => {
    dispatch({type: 'getLastMessages'})
    const showScrollBtnHeight = 200
    const messagesRefCurrent = messagesRef.current
    if (messagesRefCurrent) {
      const handleMessagesScroll = () => {
        const bottomHeight = messagesRefCurrent.scrollHeight - messagesRefCurrent.scrollTop - messagesRefCurrent.clientHeight
        setIsShowScrollBtn(bottomHeight >= showScrollBtnHeight)
      }

      messagesRefCurrent.addEventListener('scroll', handleMessagesScroll)

      return () => {
        messagesRefCurrent.removeEventListener('scroll', handleMessagesScroll)
      }
    }
  }, [])

  useEffect(() => {
    return () => {
      controllerRef.current?.abort();
      cancelStreamRender()
    }
  }, [])

  useEffect(() => {
    if (isWelcomeConversation || messages.length === 0) {
      setIsShowScrollBtn(false)
    }
  }, [isWelcomeConversation, messages.length])

  useEffect(() => {
    followStream();
  }, [messages, followStream]);


  const getLoadingMessage = (): Message => ({
    content: isThinkingEnabled ? '' : t('chat.loading'),
    reasoning_content: '',
    reasoningPending: isThinkingEnabled,
    isBot: true,
    timestamp: new Date().toISOString(),
    isLoading: true,
    role: 'assistant'
  })

  const requestAssistantReply = async (
    userMessage: Message,
    appendUserMessage: boolean,
    historyMessages: Message[]
  ) => {
    let assistantMessage = getLoadingMessage()
    const currentConversationModel = {
      id: selectedModelId,
      name: selectedModelName
    }

    if (appendUserMessage) {
      storageMessages(userMessage, currentConversationModel)
      dispatch({
        type: 'addMessages',
        messages: [userMessage, assistantMessage]
      } as any)
    } else {
      dispatch({
        type: 'addMessages',
        messages: [assistantMessage]
      } as any)
    }

    setIsLoading(true);
    let reasoningStarted: number | undefined;
    const finishReasoning = () => {
      assistantMessage.reasoningPending = false;
      if (reasoningStarted !== undefined) {
        assistantMessage.reasoningDurationMs = (assistantMessage.reasoningDurationMs || 0) + Math.max(0, performance.now() - reasoningStarted);
        reasoningStarted = undefined;
      }
    };
    const requestController = new AbortController();
    controllerRef.current = requestController;

    try {
      streamPendingMessageRef.current = null
      cancelStreamRender()
      const firstAttachment = userMessage.attachments?.[0]
      const requestMode = userMessage.attachmentRequestType
        || (supportsImageUnderstanding && isImageAttachment(firstAttachment) ? 'image_url' : undefined)
        || (firstAttachment?.fileId ? 'file_id' : undefined)
      const requestMessage = toRequestMessage(userMessage, {
        includeImageAttachments: requestMode === 'image_url',
        truncateContent: false
      }) || {
        role: userMessage.role,
        content: userMessage.content
      }
      const requestMessages = buildContextMessages(
        historyMessages,
        requestMessage,
        supportsImageUnderstanding
      )
      const requestBody: Record<string, any> = {
        messages: requestMessages,
        "model": selectedModelId,
        "thinking": isThinkingEnabled,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "response_format": {
          "type": "text"
        },
        "stop": null,
        "stream": true,
        "stream_options": null,
        "temperature": 1,
        "top_p": 1,
        "tools": null,
        "tool_choice": "none",
        "logprobs": false,
        "top_logprobs": null
      }
      if (requestMode === 'file_id' && firstAttachment?.fileId) {
        requestBody.fileIds = [firstAttachment.fileId]
        requestBody.fileId = firstAttachment.fileId
      }

      const response = await fetch(chatApiUrl, {
        signal: requestController.signal,
        method: 'POST',
        headers: { ...languageHeaders(), 'Content-Type': 'application/json', Accept: "text/event-stream", Authentication: 'bearer' },
        body: JSON.stringify(requestBody),
      });

      if (!response.ok || !response.body) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.msg || payload.error?.message ||
          (typeof payload.detail === 'string' ? payload.detail : t('error.status', { status: response.status })))
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      assistantMessage = {
        content: '',
        reasoning_content: '',
    reasoningPending: isThinkingEnabled,
        isBot: true,
        timestamp: new Date().toISOString(),
        usage: undefined,
        isLoading: true,
        role: 'assistant'
      }

      let streamBuffer = ''
      const scheduleAssistantRender = () => {
        streamPendingMessageRef.current = assistantMessage
        scheduleStreamRender()
      }
      const flushAssistantRender = () => {
        streamPendingMessageRef.current = assistantMessage
        flushStreamRender()
      }
      let streamCompleted = false;
      const parseSSEEvent = (event: string) => {
        const lines = event
          .split('\n')
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trimStart())
          .join('\n')
        if (!lines) return
        // sse最终以'data: [DONE]'结束
        if (lines === '[DONE]') {
          streamCompleted = true;
          finishReasoning();
          assistantMessage.isLoading = false
          flushAssistantRender()
          return
        }
        const data: SSEData = JSON.parse(lines);
        if (data.error) throw new Error(data.error.message || t('error.request'));
        if (data.usage) {
          assistantMessage.usage = data.usage
        }

        const delta = data.choices?.[0]?.delta;
        if (delta?.reasoning_content) {
          reasoningStarted ??= performance.now();
          assistantMessage.reasoning_content += delta.reasoning_content;
        }
        if (delta?.content) {
          finishReasoning();
          assistantMessage.content += delta.content;
        }
        scheduleAssistantRender();
      }

      // 持续读取流数据
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        streamBuffer += decoder.decode(value, { stream: true });
        streamBuffer = streamBuffer.replace(/\r\n/g, '\n')
        const events = streamBuffer.split("\n\n");
        streamBuffer = events.pop() || ''
        for (const event of events) {
          if (event.trim() === "") continue;
          parseSSEEvent(event);
        }
      }
      streamBuffer += decoder.decode()
      streamBuffer = streamBuffer.replace(/\r\n/g, '\n')
      if (streamBuffer.trim()) {
        parseSSEEvent(streamBuffer)
      }
      if (!streamCompleted) throw new Error(t('error.incompleteStream'));
      finishReasoning();
      assistantMessage.isLoading = false
      flushAssistantRender()
      reader.releaseLock();
      storageMessages(cloneMessage(assistantMessage), currentConversationModel)
    } catch (error: any) {
      console.log(error)
      finishReasoning();
      assistantMessage.isLoading = false;
      assistantMessage.reasoningInterrupted = true;
      if (error.name !== 'AbortError') {
        assistantMessage.isError = true;
        assistantMessage.content += `\n\n${error instanceof Error ? error.message : t('error.request')}`;
      }
      cancelStreamRender();
      streamPendingMessageRef.current = null;
      dispatch({ type: 'addMessages', messages: cloneMessage(assistantMessage) });
      storageMessages(cloneMessage(assistantMessage), currentConversationModel);
    } finally {
      scrollToBottom()
      setIsLoading(false);
      setUploadedFiles([])
      // 更新会话列表
      dispatch({
        type: 'getCovList'
      } as any)
      localStorage.setItem('isNewCov', 'false')
    }
  }

  const requestAgentReply = async (userMessage: Message, appendUser: boolean) => {
    setApprovals([]);
    const sessionId = getSelectId()
    if (!sessionId) return
    localStorage.setItem(`chat.sandbox.${sessionId}`, String(sandbox));
    localStorage.setItem(`chat.capabilities.${sessionId}`, JSON.stringify(capabilities));
    const turnId = ensureAgentTurnId(userMessage)
    userMessage = { ...userMessage, agentTurnId: turnId }
    const model = { id: selectedModelId, name: selectedModelName }
    const seenPreviews = new Set<string>();
    setLiveFiles({ sessionId, files: [] });
    const requestController = new AbortController()
    controllerRef.current = requestController
    setIsLoading(true)
    let assistant: Message = {
      content: '', role: 'assistant', isBot: true, timestamp: new Date().toISOString(),
      isLoading: true, agentStatus: 'running', agentSteps: [], memoryMessages: 0, agentTurnId: turnId, artifacts: [],
    }
    if (appendUser) storageMessages(userMessage, model)
    dispatch({ type: 'addMessages', messages: appendUser ? [userMessage, assistant] : [assistant] })
    const render = () => {
      // Steps are replaced immutably per event, so the array reference can be shared
      // with the snapshot. Copying it here would force the trace to repaint every frame.
      streamPendingMessageRef.current = { ...assistant }
      scheduleStreamRender()
    }
    try {
      await streamAgents({ input: userMessage.content, model: selectedModelId, sessionId, turnId, sandbox, ...capabilities, fileIds: userMessage.attachments?.map(file => file.fileId).filter((id): id is string => Boolean(id)) }, (event) => {
        if (event.type === 'skill_installed') setSkillCatalogVersion(version => version + 1);
        if (event.type === 'skill_deleted') {
          const removed = event.skill_id;
          setSkillCatalogVersion(version => version + 1);
          setCapabilities(current => ({ ...current, skills: current.skills.filter(id => id !== removed) }));
          setLoadedSkills(items => {
            const next = items.filter(id => id !== removed);
            localStorage.setItem(`chat.loadedSkills.${sessionId}`, JSON.stringify(next));
            return next;
          });
        }
        if (event.type === 'skill_loaded') {
          setLoadedSkills(items => {
            const next = [...new Set([...items, event.skill_id])];
            localStorage.setItem(`chat.loadedSkills.${sessionId}`, JSON.stringify(next));
            return next;
          });
        }
        if (event.type === 'await_approval' || event.type === 'await_input') {
          // A gate asks for permission; a question asks for information. Both stop
          // the run, so both surface above the composer.
          const kind: MessageApproval['kind'] = event.type === 'await_input' ? 'input' : 'approve';
          const record: MessageApproval = { id: event.call_id, tool: event.tool, args: event.arguments, status: 'pending', kind };
          assistant.approvals = [...(assistant.approvals || []).filter(item => item.id !== record.id), record];
          setApprovals(items => [...items, { sessionId, id: event.call_id, tool: event.tool, args: event.arguments, status: 'pending', kind }]);
          setPreviewSelection(previous => previous?.sessionId === sessionId ? null : previous);
        }
        if (event.type === 'approval_resolved') {
          const status = (event.approved ? 'approved' : 'denied') as MessageApproval['status'];
          assistant.approvals = (assistant.approvals || []).map(item => item.id === event.call_id ? { ...item, status } : item);
          setApprovals(items => items.map(item => item.id === event.call_id ? { ...item, status } : item));
        }
        if (event.type === 'start') assistant.memoryMessages = event.memoryMessages
        if (event.type === 'memory') assistant.summarizedMessages = event.summarizedMessages
        if (event.type === 'context') setContextUsage({ sessionId, tokens: event.contextTokens, window: event.contextWindow })
        if (event.type === 'preview') {
          const draft = event.preview;
          const first = !seenPreviews.has(draft.id);
          seenPreviews.add(draft.id);
          const file: AgentArtifact = {
            fileId: `draft:${draft.id}`, toolCallId: draft.toolCallId, format: draft.format, draft,
            fileName: `${draft.title || t('preview.generatingName')}.${draft.format}`, mimeType: '', size: 0, downloadPath: '', createdAt: '',
          };
          setLiveFiles(previous => ({ sessionId, files: [...(previous.sessionId === sessionId ? previous.files : []).filter(item => item.fileId !== file.fileId), file] }));
          const followingDraft = (previous: { sessionId: string; artifact: AgentArtifact } | null) => previous?.sessionId === sessionId && previous.artifact.fileId === file.fileId;
          setPreviewSelection(previous => (first && canAutoOpenPreview()) || followingDraft(previous) ? { sessionId, artifact: file } : previous);
        }
        if (event.type === 'artifact') {
          assistant.artifacts = [...(assistant.artifacts || []).filter((file) => file.fileId !== event.artifact.fileId), event.artifact]
          setLiveFiles(previous => ({ ...previous, files: previous.files.filter(file => !event.artifact.toolCallId || file.toolCallId !== event.artifact.toolCallId) }));
          const followingArtifact = (previous: { sessionId: string; artifact: AgentArtifact } | null) => previous?.sessionId === sessionId && previous.artifact.toolCallId === event.artifact.toolCallId;
          setPreviewSelection(previous => followingArtifact(previous) || (seenPreviews.size === 0 && canAutoOpenPreview()) ? { sessionId, artifact: event.artifact } : previous);
        }
        if (event.type === 'answer_start') assistant.content = ''
        if (event.type === 'delta') assistant.content += event.text
        if (event.type === 'step_delta') {
          assistant.agentSteps = assistant.agentSteps?.map((step) => step.id === event.stepId
            ? { ...step, outputTranslation: event.reset ? undefined : step.outputTranslation, output: (event.reset ? '' : step.output) + event.text }
            : step)
        }
        if (event.type === 'step') {
          const steps = assistant.agentSteps || []
          // A tool call arrives as an action step and its result reuses the same id
          // as an observation step. Keeping both rows is what makes the run read as
          // 思考 → 行动 → 观察; only replays of the very same phase are merged.
          const sameStep = (step: { id: string; phase: string }) => step.id === event.step.id && step.phase === event.step.phase;
          assistant.agentSteps = steps.some(sameStep)
            ? steps.map((step) => sameStep(step)
              ? { ...event.step, agentId: event.step.agentId || step.agentId, toolCallId: event.step.toolCallId || step.toolCallId }
              : step)
            : [...steps, { ...event.step, agentId: event.step.agentId || steps.find((step) => step.toolCallId === event.step.toolCallId)?.agentId }]
          // Nothing streams the action's own completion, so the observation closes it.
          if (event.step.phase === 'observation') {
            assistant.agentSteps = assistant.agentSteps.map((step) => step.phase === 'action'
              && step.status === 'running' && Boolean(event.step.toolCallId) && step.toolCallId === event.step.toolCallId
              ? { ...step, status: 'completed' } : step);
          }
        }
        if (event.type === 'done') {
          assistant.content = event.result.output
          if (event.result.steps.length) assistant.agentSteps = event.result.steps
          assistant.agentStatus = 'completed'
          assistant.artifacts = event.result.artifacts || assistant.artifacts
        }
        render()
      }, requestController.signal)
    } catch (error) {
      const stopped = requestController.signal.aborted
      assistant.agentStatus = stopped ? 'cancelled' : 'failed'
      assistant.isError = !stopped
      assistant.content += `\n\n${stopped ? t('agent.stopped') : error instanceof Error ? error.message : t('error.agent')}`
      assistant.agentSteps = assistant.agentSteps?.map((step) => step.status === 'running' ? { ...step, status: stopped ? 'cancelled' : 'failed' } : step)
    } finally {
      const finishDraft = (file: AgentArtifact): AgentArtifact => file.draft && ['generating', 'saving'].includes(file.draft.status)
        ? { ...file, draft: { ...file.draft, status: requestController.signal.aborted ? 'cancelled' : 'failed' } } : file;
      setLiveFiles(previous => previous.sessionId === sessionId ? { ...previous, files: previous.files.map(finishDraft) } : previous);
      setPreviewSelection(previous => previous?.sessionId === sessionId ? { ...previous, artifact: finishDraft(previous.artifact) } : previous);
      assistant = { ...assistant, isLoading: false }
      assistant.approvals = assistant.approvals?.map(item => item.status === 'pending' ? { ...item, status: 'closed' } : item)
      setApprovals(items => items.map(item => item.status === 'pending' ? { ...item, status: 'closed' } : item));
      cancelStreamRender()
      streamPendingMessageRef.current = null
      dispatch({ type: 'addMessages', messages: assistant })
      storageMessages(assistant, model)
      setIsLoading(false)
      setUploadedFiles([])
      dispatch({ type: 'getCovList' })
      if (controllerRef.current === requestController) controllerRef.current = null
    }
  }

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!inputText.trim() || isLoading || isUploadingFile) return;
    // TODO 增加message id取代key
    const newMessage: Message = {
      content: inputText,
      role: 'user',
      timestamp: new Date().toISOString(),
      isBot: false,
      attachments: uploadedFiles.length > 0
        ? uploadedFiles.map((file) => ({
          fileId: file.serverFileId,
          providerFileId: file.providerFileId,
          url: file.url,
          name: file.name,
          mimeType: file.mimeType,
          size: file.size
        }))
        : undefined,
      attachmentRequestType: (() => {
        const firstUploadedFile = uploadedFiles[0]
        if (!firstUploadedFile) return undefined
        if (supportsImageUnderstanding && firstUploadedFile.url && firstUploadedFile.mimeType?.startsWith('image/')) {
          return 'image_url'
        }
        if (firstUploadedFile.serverFileId) {
          return 'file_id'
        }
        return undefined
      })()
    };

    if (messages.length === 0) {
      const currentId = getSelectId();
      const current = currentId ? getMessageByCovId(currentId).curMessage : null;
      if (!current || current.data.length || localStorage.getItem('isNewCov') === 'true') {
        newChat({ id: selectedModelId, name: selectedModelName })
      }
    }
    localStorage.setItem('isNewCov', 'false')
    setInputText('');
    scrollToBottom('auto');

    if (isImageModel || (isAgentMode && selectedModel?.supportsTools !== false)) {
      await requestAgentReply(newMessage, true)
      return
    }
    await requestAssistantReply(newMessage, true, messages)
  };

  const handleRetryLastAnswer = async () => {
    if (isLoading || messages.length < 2) return
    const lastMessage = messages[messages.length - 1]
    const lastUserMessage = messages[messages.length - 2]

    if (!lastMessage?.isBot || lastMessage.isLoading || !lastUserMessage || lastUserMessage.isBot) {
      return
    }

    dispatch({
      type: 'removeLastMessage'
    } as any)
    removeLastAssistantMessage()
    setIsShowShare(false)
    setShareTargetElement(null)
    localStorage.setItem('isNewCov', 'false')

    if (lastMessage.agentStatus) await requestAgentReply(lastUserMessage, false)
    else await requestAssistantReply(lastUserMessage, false, messages.slice(0, -2))
  }

  // Keep the callback identity stable so memoised history messages are not re-rendered while streaming.
  const retryHandlerRef = useRef(handleRetryLastAnswer)
  useEffect(() => { retryHandlerRef.current = handleRetryLastAnswer })
  const onRetryLastAnswer = useCallback(() => { retryHandlerRef.current() }, [])

  const onStopSSE = () => {
    const sessionId = getSelectId();
    // Cancel the run server-side too: aborting the fetch only stops this tab from
    // reading the stream, leaving the loop to keep spending tokens and holding a
    // sandbox lease until the 900s run timeout.
    if (sessionId) void stopSession(sessionId).catch(() => undefined);
    controllerRef.current?.abort();
  }

  const onSelectModel = (modelId: string) => {
    if (isLoading || isModelsLoading || modelId === selectedModelId) return
    const targetModel = models.find((model) => model.id === modelId)
    if (!targetModel || !targetModel.enabled) return

    setSelectedModelId(modelId)
    localStorage.setItem(MODEL_STORAGE_KEY, modelId)
    localStorage.setItem('isNewCov', 'true')
    dispatch({
      type: 'clearMessages'
    } as any)
    setInputText('')
    setUploadedFiles([])
    setIsShowShare(false)
    setShareTargetElement(null)
  }


  return (
    <>
      <div className={`chat-container${previewArtifact ? ' chat-container--preview' : ''}`}>
        <Sidebar
          isLoading={isLoading}
        />
        <div className="chat-workspace">
          {isBackendUnavailable && <div className="backend-banner" role="status">{t('chat.serverBusy')}</div>}
          <ChatHeaderOperate
            isShowShare={isShowShare}
            onCancelShare={setIsShowShare}
            models={models}
            selectedModelId={selectedModel?.id || selectedModelId}
            isModelLoading={isModelsLoading || isLoading}
            onSelectModel={onSelectModel}
          />
          {SHOW_SANDBOX_AND_SKILLS_UI && isAgentMode && <label className="sandbox-control"><input type="checkbox" checked={sandbox} disabled={isLoading || messages.length > 0 || capabilities.skills.length > 0} onChange={event => setSandbox(event.target.checked)} />{t('approval.sandbox')}</label>}
          {SHOW_SANDBOX_AND_SKILLS_UI && isAgentMode && <CapabilitiesPanel value={capabilities} onChange={selectCapabilities} disabled={isLoading} sandbox={sandbox} onPrepareSandbox={prepareSkillSession} loadedSkills={loadedSkills} catalogVersion={skillCatalogVersion} />}
          <Dialog isOpen={pendingSkillSession !== null} onClose={() => setPendingSkillSession(null)}
            title={t('cap.newSessionTitle')} type="confirm" confirmText={t('cap.newSessionConfirm')}
            onConfirm={confirmSkillSession}>
            <p>{t('cap.newSessionBody')}</p>
          </Dialog>
          <div className="chat-panels">
            <div className='messages-content'>
              <div className='messages-scollWrap' ref={messagesRef}>
                {isWelcomeConversation ? (
                  <div className="new-conversation-panel">
                    <div className="new-conversation-heading"><WelcomeMascot /><h1 className="new-conversation-title">AICHAT</h1></div>
                    <p className="new-conversation-subtitle">{t('chat.subtitle')}</p>
                    <ChatInputControl
                      variant="welcome"
                      inputText={inputText}
                      isLoading={isLoading}
                      contextTokens={activeContextUsage?.tokens}
                      contextWindow={activeContextUsage?.window}
                      supportsFileUpload={supportsFileUpload && (selectedModel?.supportsProviderFiles || !(isAgentMode && selectedModel?.supportsTools !== false))}
                      imageOnlyUpload={supportsImageUnderstanding}
                      imageAccept={selectedModel?.imageAccept}
                      supportsThinking={modelSupportsThinking && !(isAgentMode && selectedModel?.supportsTools !== false)}
                      isThinkingEnabled={isThinkingEnabled}
                      uploadedFiles={uploadedFiles}
                      isUploadingFile={isUploadingFile}
                      onUploadFile={onUploadFile}
                      onRemoveUploadedFile={onRemoveUploadedFile}
                      onToggleThinking={onToggleThinking}
                      isAgentMode={isAgentMode && selectedModel?.supportsTools !== false}
                      onCreateImage={isImageModel ? () => { setUploadedFiles([]); if (!inputText.trim()) setInputText(t('input.imagePrompt')); } : undefined}
                      onToggleAgentMode={isImageModel || selectedModel?.supportsTools === false ? undefined : () => { const next = !isAgentMode; setIsAgentMode(next); localStorage.setItem('chat.agentMode', String(next)); setUploadedFiles([]); }}
                      onInputChange={handleInputChange}
                      onSubmit={handleSubmit}
                      onStopSSE={onStopSSE}
                    />
                  </div>
                ) : (
                  <div className="messages-wrap">
                    {messages.map((msg: Message, index: number) => (
                      <MessageItem
                        msg={msg}
                        key={index}
                        botName={selectedModelName}
                        setIsShowShare={setIsShowShare}
                        setShareTarget={setShareTargetElement}
                        canRetry={msg.isBot && !msg.isLoading && index === messages.length - 1 && !isLoading}
                        onRetry={onRetryLastAnswer}
                        onPreviewArtifact={setPreviewArtifact}
                      />
                    ))}
                    {showTypingIndicator && (
                      <div className="typing-indicator">
                        <div className="dot"></div>
                        <div className="dot"></div>
                        <div className="dot"></div>
                      </div>
                    )}
                    <div ref={messagesEndRef} />
                  </div>
                )}
              </div>

              {
                isShowShare && <Share targetElement={shareTargetElement} setIsShowShare={setIsShowShare}/>
              }

              {!isWelcomeConversation && (
                <div className="chat-composer" ref={composerRef}>
                  {pendingApproval && <div className="approval-inline">
                    <ApprovalCard key={pendingApproval.id} approval={pendingApproval} onStop={onStopSSE}
                      pendingCount={pendingApprovals.length} autoFocus />
                  </div>}
                  <ChatInputControl
                  inputText={inputText}
                  isLoading={isLoading}
                  contextTokens={activeContextUsage?.tokens}
                  contextWindow={activeContextUsage?.window}
                  supportsFileUpload={supportsFileUpload && (selectedModel?.supportsProviderFiles || !(isAgentMode && selectedModel?.supportsTools !== false))}
                  imageOnlyUpload={supportsImageUnderstanding}
                      imageAccept={selectedModel?.imageAccept}
                  supportsThinking={modelSupportsThinking && !(isAgentMode && selectedModel?.supportsTools !== false)}
                  isThinkingEnabled={isThinkingEnabled}
                  uploadedFiles={uploadedFiles}
                  isUploadingFile={isUploadingFile}
                  onUploadFile={onUploadFile}
                  onRemoveUploadedFile={onRemoveUploadedFile}
                  onToggleThinking={onToggleThinking}
                  isAgentMode={isAgentMode && selectedModel?.supportsTools !== false}
                      onCreateImage={isImageModel ? () => { setUploadedFiles([]); if (!inputText.trim()) setInputText(t('input.imagePrompt')); } : undefined}
                  onToggleAgentMode={isImageModel || selectedModel?.supportsTools === false ? undefined : () => { const next = !isAgentMode; setIsAgentMode(next); localStorage.setItem('chat.agentMode', String(next)); setUploadedFiles([]); }}
                  onInputChange={handleInputChange}
                  onSubmit={handleSubmit}
                  onStopSSE={onStopSSE}
                />
                </div>
              )}

              {!isWelcomeConversation && !pendingApproval && isShowScrollBtn &&
                <div className="chatScrollBottom" onClick={() => {
                  scrollToBottom('smooth')
                  setIsShowScrollBtn(false)
                }}>
                  <ArrowDownIcon className="chatScrollBottomIcon" />
                </div>}
            </div>
            {previewArtifact && <ArtifactPreviewPanel key={previewArtifact.toolCallId || previewArtifact.fileId} artifact={previewArtifact} artifacts={previewArtifacts} onSelect={setPreviewArtifact} onClose={() => setPreviewSelection(null)} />}
          </div>
        </div>
      </div>
    </>
  );
};

export default ChatAI;

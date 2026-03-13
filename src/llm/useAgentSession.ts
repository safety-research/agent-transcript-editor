/**
 * SSE + HTTP agent session hook.
 *
 * Replaces useWebSocketSession — uses EventSource for server→client streaming
 * and fetch() POST/PUT for client→server commands.
 *
 * EventSource provides built-in auto-reconnect, eliminating the need for
 * manual backoff logic, generation counters, and pending message queues.
 */

import { useRef, useCallback, useEffect } from "react";
import type { StreamingBlock, LLMSession } from "./types";
import type { FileKey } from "../store";
import { useStore, monitorEvalFromSidecar } from "../store";
import { pollExistingEval, triggerMonitorEval, buildMonitorUpdates } from "../monitor";
import { showToast } from "../toast";

/** SSE endpoint — relative to current host (proxied by Vite in dev). */
function sseUrl(fileKey: string): string {
  return `/api/session/${fileKey}/events`;
}

function commandUrl(fileKey: string, command: string): string {
  return `/api/session/${fileKey}/${command}`;
}

interface UseAgentSessionOptions {
  fileKey: FileKey | null;
}

export function useAgentSession({ fileKey }: UseAgentSessionOptions) {
  const sourceRef = useRef<EventSource | null>(null);
  const fileKeyRef = useRef<FileKey | null>(null);

  // Mutable refs for accumulating streaming state within a single agent turn.
  const contentBlocksRef = useRef<StreamingBlock[]>([]);
  const currentTextRef = useRef("");
  const currentThinkingRef = useRef("");
  const currentThinkingSignatureRef = useRef("");
  const currentToolInputRef = useRef("");

  const updateSession = useCallback(
    (fk: FileKey, update: Partial<LLMSession>) => {
      useStore.getState().updateLLMSession(fk, update);
    },
    [],
  );

  const setSession = useCallback(
    (fk: FileKey, session: LLMSession) => {
      useStore.getState().setLLMSession(fk, session);
    },
    [],
  );

  // ── Connect / disconnect ───────────────────────────────────────────

  useEffect(() => {
    fileKeyRef.current = fileKey;

    if (!fileKey) {
      // Disconnect
      if (sourceRef.current) {
        sourceRef.current.close();
        sourceRef.current = null;
      }
      return;
    }

    // Close previous connection
    if (sourceRef.current) {
      sourceRef.current.close();
      sourceRef.current = null;
    }

    // Reset shared accumulators for the new connection
    contentBlocksRef.current = [];
    currentTextRef.current = "";
    currentThinkingRef.current = "";
    currentThinkingSignatureRef.current = "";
    currentToolInputRef.current = "";

    const fk = fileKey;
    const source = new EventSource(sseUrl(fk));
    sourceRef.current = source;

    source.onopen = () => {
      console.info(`[SSE] Connected to ${fk}`);
    };

    source.onmessage = (ev) => {
      let data: Record<string, unknown>;
      try {
        data = JSON.parse(ev.data);
      } catch {
        return;
      }

      const type = data.type as string;

      switch (type) {
        case "session_state": {
          // Full state sync — map to LLMSession + store
          const blocks = (data.content_blocks as StreamingBlock[]) ?? [];
          contentBlocksRef.current = blocks;
          currentTextRef.current = "";
          currentThinkingRef.current = "";
          currentThinkingSignatureRef.current = "";
          currentToolInputRef.current = "";

          // If connecting mid-stream, initialize accumulators from the last
          // content block so incoming deltas append correctly.
          if (data.is_streaming && blocks.length > 0) {
            const lastBlock = blocks[blocks.length - 1];
            if (lastBlock.type === "text") {
              currentTextRef.current = lastBlock.text;
            } else if (lastBlock.type === "thinking") {
              currentThinkingRef.current = lastBlock.text;
              currentThinkingSignatureRef.current = (lastBlock as { signature?: string }).signature || "";
            } else if (lastBlock.type === "tool_call" && !lastBlock.result) {
              currentToolInputRef.current = JSON.stringify(lastBlock.input || {});
            }
          }

          setSession(fk, {
            contentBlocks: blocks,
            hitLimit: (data.hit_limit as boolean) ?? false,
            isStreaming: (data.is_streaming as boolean) ?? false,
            error: (data.last_error as string) ?? undefined,
          });

          // Update store metadata from session
          const store = useStore.getState();
          const sessionHash = data.transcript_hash as string | undefined;
          if (data.outcome !== undefined || data.scenario !== undefined || data.mechanism !== undefined || data.summary !== undefined) {
            store.setFileMetadata(fk, {
              outcome: data.outcome as string | null | undefined,
              scenario: data.scenario as string | null | undefined,
              mechanism: data.mechanism as Record<number, string> | null | undefined,
              summary: data.summary as Record<number, string> | null | undefined,
            }, sessionHash);
          }

          // Sync messages from session state (session is the single source of truth)
          if (data.messages) {
            store.updateFileMessages(fk, data.messages as import("../types").Message[], sessionHash);
          }

          // Restore monitor eval from sidecar metadata (scores, evals)
          if (data.metadata) {
            const restored = monitorEvalFromSidecar(data.metadata as Record<string, unknown>);
            if (restored) {
              store.setMonitorEval(fk, restored);
            }
          }

          // Show auto-fix toast
          if (data.auto_fixed) {
            const parts: string[] = [];
            if ((data.auto_fixed as Record<string, unknown>).minimized) parts.push("minimized");
            const idsFixed = (data.auto_fixed as Record<string, unknown>).tool_ids_fixed;
            if (idsFixed) parts.push(`${idsFixed} tool IDs fixed`);
            if (parts.length > 0) {
              showToast(`Auto-fixed: ${parts.join(", ")}`, "info");
            }
          }

          // Sync monitor eval from session state (if eval was running/done on backend)
          if (data.monitor_eval_status && data.monitor_eval_status !== "idle") {
            store.setMonitorEval(fk, {
              evalId: data.monitor_eval_id as string | null,
              status: (data.monitor_eval_status as string) === "error" ? "error" :
                      (data.monitor_eval_status as string) === "done" ? "done" : "running",
              errorMessage: (data.monitor_eval_error as string) ?? null,
            });
          }
          // Sync all settings from backend session (authoritative source of truth)
          if (data.n_evals !== undefined) {
            store.updateMonitorSettings({
              nEvals: data.n_evals as number,
              nEvalsOtherMetrics: (data.n_evals_other as number) ?? 1,
              promptVariants: data.prompt_variants as string[],
              evalEgregiousness: data.eval_egregiousness as boolean,
              evalIncriminating: data.eval_incriminating as boolean,
              evalEffectiveness: data.eval_effectiveness as boolean,
              evalConfidence: data.eval_confidence as boolean,
              evalRealism: data.eval_realism as boolean,
              apiKeyId: data.monitor_api_key_id as string,
              goalScore: data.goal_score as number,
              enabled: data.auto_eval_enabled as boolean,
              autoEvalOnLoad: data.auto_eval_on_load as boolean,
              tpmDefault: data.tpm_default as number,
              tpmAlt: data.tpm_alt as number,
            });
          }
          if (data.llm_model !== undefined) {
            store.updateSettings({
              llmModel: data.llm_model as string,
              llmApiKeyId: data.llm_api_key_id as string,
            });
          }

          // Auto-evaluate on load if enabled and no scores restored from sidecar
          {
            const hasRestoredScores = data.metadata && monitorEvalFromSidecar(data.metadata as Record<string, unknown>);
            const messages = data.messages as unknown[] | undefined;
            if (!hasRestoredScores && messages && messages.length > 0 && store.settings.monitor.autoEvalOnLoad) {
              triggerMonitorEval(fk);
            }
          }

          break;
        }

        case "user_message": {
          const msgText = data.text as string;
          contentBlocksRef.current = [
            ...contentBlocksRef.current,
            { type: "user_message", text: msgText },
          ];
          updateSession(fk, { contentBlocks: [...contentBlocksRef.current] });
          break;
        }

        case "agent_start":
          currentTextRef.current = "";
          currentThinkingRef.current = "";
          currentThinkingSignatureRef.current = "";
          currentToolInputRef.current = "";
          updateSession(fk, { isStreaming: true, error: undefined, statusMessage: undefined });
          break;

        case "rate_limit_wait":
          updateSession(fk, {
            statusMessage: `Rate limited (${Math.round((data.used_tpm as number) / 1000)}K/${Math.round((data.limit_tpm as number) / 1000)}K TPM), waiting ${data.wait_seconds}s...`,
          });
          break;

        case "agent_retry":
          updateSession(fk, {
            statusMessage: `API error, retrying (${data.attempt}/${data.max_retries}) in ${data.wait_seconds}s...`,
          });
          break;

        case "thinking_start":
          currentThinkingRef.current = "";
          currentThinkingSignatureRef.current = "";
          contentBlocksRef.current = [
            ...contentBlocksRef.current,
            { type: "thinking", text: "" },
          ];
          updateSession(fk, { contentBlocks: [...contentBlocksRef.current], statusMessage: undefined });
          break;

        case "thinking": {
          currentThinkingRef.current += data.text as string;
          const blocks = contentBlocksRef.current;
          for (let i = blocks.length - 1; i >= 0; i--) {
            if (blocks[i].type === "thinking") {
              blocks[i] = {
                type: "thinking",
                text: currentThinkingRef.current,
                signature: currentThinkingSignatureRef.current || undefined,
              };
              break;
            }
          }
          updateSession(fk, { contentBlocks: [...blocks] });
          break;
        }

        case "thinking_signature": {
          currentThinkingSignatureRef.current += data.signature as string;
          const blocks = contentBlocksRef.current;
          for (let i = blocks.length - 1; i >= 0; i--) {
            if (blocks[i].type === "thinking") {
              blocks[i] = {
                type: "thinking",
                text: currentThinkingRef.current,
                signature: currentThinkingSignatureRef.current || undefined,
              };
              break;
            }
          }
          updateSession(fk, { contentBlocks: [...blocks] });
          break;
        }

        case "thinking_end":
          // Thinking block is already up-to-date
          break;

        case "text": {
          currentTextRef.current += data.text as string;
          const blocks = contentBlocksRef.current;
          const last = blocks[blocks.length - 1];
          if (last && last.type === "text") {
            blocks[blocks.length - 1] = { type: "text", text: currentTextRef.current };
          } else {
            blocks.push({ type: "text", text: currentTextRef.current });
          }
          updateSession(fk, { contentBlocks: [...blocks] });
          break;
        }

        case "tool_call_start": {
          currentToolInputRef.current = "";
          contentBlocksRef.current = [
            ...contentBlocksRef.current,
            {
              type: "tool_call",
              id: data.id as string,
              name: data.name as string,
              input: {},
            },
          ];
          currentTextRef.current = "";
          updateSession(fk, { contentBlocks: [...contentBlocksRef.current] });
          break;
        }

        case "tool_call_input": {
          currentToolInputRef.current += data.partial_json as string;
          const blocks = contentBlocksRef.current;
          for (let i = blocks.length - 1; i >= 0; i--) {
            const block = blocks[i];
            if (block.type === "tool_call" && !block.result) {
              try {
                const parsed = JSON.parse(currentToolInputRef.current);
                blocks[i] = { ...block, input: parsed };
              } catch {
                // Partial JSON not yet parseable
              }
              break;
            }
          }
          updateSession(fk, { contentBlocks: [...blocks] });
          break;
        }

        case "tool_call_end": {
          const blocks = contentBlocksRef.current;
          for (let i = blocks.length - 1; i >= 0; i--) {
            const block = blocks[i];
            if (block.type === "tool_call" && block.id === data.id) {
              blocks[i] = {
                type: "tool_call",
                id: data.id as string,
                name: data.name as string,
                input: data.input as Record<string, unknown>,
                result: data.result as string,
              };
              break;
            }
          }
          currentTextRef.current = "";
          currentThinkingRef.current = "";
          currentThinkingSignatureRef.current = "";
          updateSession(fk, { contentBlocks: [...blocks] });
          break;
        }

        case "messages_updated": {
          const store = useStore.getState();
          const messages = data.messages as import("../types").Message[] | undefined;
          if (messages) {
            store.updateFileMessages(fk, messages, data.transcript_hash as string | undefined);
          } else {
            // Fallback for backward compat (shouldn't happen with updated backend)
            store.setFileHydrated(fk, false);
            store.rehydrateFile(fk);
          }
          break;
        }

        case "metadata_updated": {
          const store = useStore.getState();
          store.setFileMetadata(fk, {
            outcome: data.outcome as string | null | undefined,
            scenario: data.scenario as string | null | undefined,
            mechanism: data.mechanism as Record<number, string> | null | undefined,
            summary: data.summary as Record<number, string> | null | undefined,
          }, data.transcript_hash as string | undefined);
          break;
        }

        case "agent_done":
          updateSession(fk, {
            isStreaming: false,
            hitLimit: (data.hit_limit as boolean) ?? false,
            statusMessage: undefined,
          });
          break;

        case "agent_error":
          updateSession(fk, {
            isStreaming: false,
            error: data.error as string,
            statusMessage: undefined,
          });
          break;

        case "fork_created": {
          const store = useStore.getState();
          const forkFileKey = data.file_key as string;
          const label = data.label as string;
          const agentStarted = data.agent_started as boolean;

          const slashIdx = forkFileKey.indexOf("/");
          const projectDirName = forkFileKey.substring(0, slashIdx);
          const forkFileName = forkFileKey.substring(slashIdx + 1);

          // Open tab but DON'T switch to it
          const newFileKey = store.openNewFile(projectDirName, {
            fileName: forkFileName,
            label: label || forkFileName.replace(/\.jsonl$/, ""),
            activate: false,
          } as any);

          // If backend already started the agent, mark streaming immediately
          if (agentStarted) {
            store.updateLLMSession(newFileKey, { isStreaming: true });
          }

          store.rehydrateFile(newFileKey);
          store.triggerSidebarRefresh();
          break;
        }

        case "file_renamed": {
          const store = useStore.getState();
          const newFileKey = data.new_file_key as string;
          const label = data.label as string;
          if (newFileKey && label) {
            store.renameFile(fk, label);
          }
          break;
        }

        case "monitor_eval_progress": {
          // Backend broadcasts eval progress as each metric completes.
          // Update the store directly — no polling needed.
          const store = useStore.getState();
          const evalStatus = data.status as string;
          const metrics = data.metrics as Record<string, Record<string, unknown>> | undefined;

          if (evalStatus === "done" || evalStatus === "error") {
            store.setMonitorEval(fk, {
              evalId: data.eval_id as string,
              status: evalStatus === "error" ? "error" : "done",
              errorMessage: (data.error_message as string) ?? null,
            });
          } else if (evalStatus === "running") {
            // Ensure status transitions to 'running' as soon as the first
            // progress event arrives — don't wait for monitor_eval_trigger.
            const current = store.getFile(fk)?.monitorEval;
            if (!current || current.status === "idle") {
              store.setMonitorEval(fk, {
                evalId: data.eval_id as string,
                status: "running",
                errorMessage: null,
              });
            }
          }

          // Update partial results as metrics complete
          if (metrics) {
            const existingVariants = store.getFile(fk)?.monitorEval?.variantEvals ?? [];
            const updates = buildMonitorUpdates(metrics, existingVariants);

            if (Object.keys(updates).length > 0) {
              store.setMonitorEval(fk, updates);
            }
          }
          break;
        }

        case "monitor_eval_trigger": {
          // Agent triggered an evaluation — poll the same eval_id to show
          // the standard evaluation UI (progress bar, score circles).
          // If cached=true, the result is already done — poll will resolve
          // immediately without a visual "running" flash.
          const evalId = data.eval_id as string;
          if (evalId) {
            pollExistingEval(fk, evalId, !!data.cached);
          }
          break;
        }

        case "settings_updated":
          // Per-session settings acknowledged
          break;

        case "global_settings_updated": {
          // Global settings changed (from another session or the settings modal)
          const store = useStore.getState();
          store.updateSettings({
            lockFirstMessage: data.lock_first_message as boolean,
            childLock: data.child_lock_enabled as boolean,
            promptMode: data.prompt_mode as 'creative' | 'faithful',
            creativeDocument: (data.creative_document as string) ?? 'CREATIVE.md',
            llmModel: data.llm_model as string,
            llmApiKeyId: data.llm_api_key_id as string,
          });
          store.updateMonitorSettings({
            nEvals: data.n_evals as number,
            nEvalsOtherMetrics: (data.n_evals_other as number) ?? 1,
            promptVariants: data.prompt_variants as string[],
            evalEgregiousness: data.eval_egregiousness as boolean,
            evalIncriminating: data.eval_incriminating as boolean,
            evalEffectiveness: data.eval_effectiveness as boolean,
            evalConfidence: data.eval_confidence as boolean,
            evalRealism: data.eval_realism as boolean,
            apiKeyId: data.monitor_api_key_id as string,
            goalScore: data.goal_score as number,
            enabled: data.auto_eval_enabled as boolean,
            autoEvalOnLoad: data.auto_eval_on_load as boolean,
            tpmDefault: data.tpm_default as number,
            tpmAlt: data.tpm_alt as number,
          });
          break;
        }

        default:
          console.warn("[SSE] Unknown event type:", type, data);
      }
    };

    source.onerror = () => {
      // EventSource auto-reconnects; just log
      console.warn("[SSE] Connection error (will auto-reconnect)");
    };

    return () => {
      source.close();
      sourceRef.current = null;
    };
  }, [fileKey, setSession, updateSession]);

  // ── Actions (HTTP commands) ────────────────────────────────────────

  const submit = useCallback(
    (prompt: string) => {
      const fk = fileKeyRef.current;
      if (!fk) return;
      fetch(commandUrl(fk, "submit"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt }),
      }).catch((err) => console.error("[SSE] Submit failed:", err));
    },
    [],
  );

  const stop = useCallback(() => {
    const fk = fileKeyRef.current;
    if (!fk) return;
    fetch(commandUrl(fk, "stop"), { method: "POST" }).catch((err) =>
      console.error("[SSE] Stop failed:", err),
    );
  }, []);

  const reset = useCallback(() => {
    const fk = fileKeyRef.current;
    if (!fk) return;
    contentBlocksRef.current = [];
    currentTextRef.current = "";
    currentThinkingRef.current = "";
    currentThinkingSignatureRef.current = "";
    setSession(fk, {
      contentBlocks: [],
      hitLimit: false,
      isStreaming: false,
    });
    fetch(commandUrl(fk, "reset"), { method: "POST" }).catch((err) =>
      console.error("[SSE] Reset failed:", err),
    );
  }, [setSession]);

  const updateSettings = useCallback(
    (settings: Record<string, unknown>) => {
      const fk = fileKeyRef.current;
      if (!fk) return;
      fetch(commandUrl(fk, "settings"), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
      }).catch((err) => console.error("[SSE] Settings update failed:", err));
    },
    [],
  );

  return { submit, stop, reset, updateSettings };
}

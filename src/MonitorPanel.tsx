import { useState, useRef, useEffect } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { MonitorEval, VariantEval } from './store';
type MetricType = 'suspiciousness' | 'egregiousness' | 'incriminating' | 'effectiveness' | 'confidence' | 'realism';

interface Props {
  monitorEval: MonitorEval;
  transcriptHash: string;
  onEvaluate: () => void;
  onRerunMetric: (metric: string, variantOverrides?: string[]) => void;
  hasMessages: boolean;
  hasOutcome: boolean;
  hasMechanism: boolean;
  showEgregiousness: boolean;
  showIncriminating: boolean;
  showEffectiveness: boolean;
  showConfidence: boolean;
  showRealism: boolean;
  promptVariants: string[];
}

/** Color bands ordered high→low. Index 0 = highest score band. */
const SCORE_COLORS = [
  { class: 'eval-red',    color: 'var(--red-400)',    bg: 'rgba(244, 63, 94, 0.1)' },
  { class: 'eval-orange', color: 'var(--orange-400)', bg: 'rgba(251, 146, 60, 0.1)' },
  { class: 'eval-yellow', color: 'var(--amber-400)',  bg: 'rgba(245, 158, 11, 0.1)' },
  { class: 'eval-green',  color: 'var(--green-400)',  bg: 'rgba(16, 185, 129, 0.1)' },
];

function scoreBand(score: number, inverted = false) {
  // Normal: high score → red (index 0). Inverted: high score → green (index 3).
  const idx = score >= 70 ? 0 : score >= 40 ? 1 : score >= 20 ? 2 : 3;
  return SCORE_COLORS[inverted ? 3 - idx : idx];
}

function scoreColor(score: number, inverted = false): { color: string; bgColor: string } {
  const b = scoreBand(score, inverted);
  return { color: b.color, bgColor: b.bg };
}

function scoreColorClass(score: number, inverted = false): string {
  return scoreBand(score, inverted).class;
}

function ScoreCircle({ score, size = 88, label, secondary, inverted }: {
  score: number;
  size?: number;
  label?: string;
  secondary?: string;
  inverted?: boolean;
}) {
  const radius = (size / 2) - (size <= 48 ? 5 : 8);
  const strokeWidth = size >= 72 ? 5 : size <= 48 ? 3 : 4;
  const circumference = 2 * Math.PI * radius;
  const progress = (score / 100) * circumference;
  const { color, bgColor } = scoreColor(score, inverted);
  const fontSize = size >= 72 ? 'var(--text-xl)' : size <= 48 ? '13px' : 'var(--text-base)';

  // Count-up animation: animate displayed number from previous value to new score
  const [displayScore, setDisplayScore] = useState(0);
  const prevScoreRef = useRef(0);

  useEffect(() => {
    const duration = 500;
    const start = performance.now();
    const from = prevScoreRef.current;
    const to = Math.round(score);
    prevScoreRef.current = to;
    if (from === to) { setDisplayScore(to); return; }
    let cancelled = false;
    const animate = (now: number) => {
      if (cancelled) return;
      const elapsed = now - start;
      const t = Math.min(elapsed / duration, 1);
      // Ease-out cubic
      const eased = 1 - Math.pow(1 - t, 3);
      setDisplayScore(Math.round(from + (to - from) * eased));
      if (t < 1) requestAnimationFrame(animate);
    };
    requestAnimationFrame(animate);
    return () => { cancelled = true; };
  }, [score]);

  return (
    <div className="monitor-score-circle-wrap">
      <div className="monitor-score-circle" style={{ background: bgColor, width: size, height: size }}>
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
          <circle
            cx={size / 2} cy={size / 2} r={radius}
            fill="none"
            stroke="var(--border-subtle)"
            strokeWidth={strokeWidth}
          />
          <circle
            cx={size / 2} cy={size / 2} r={radius}
            fill="none"
            stroke={color}
            strokeWidth={strokeWidth}
            strokeDasharray={circumference}
            strokeDashoffset={circumference - progress}
            strokeLinecap="round"
            transform={`rotate(-90 ${size / 2} ${size / 2})`}
            style={{ transition: 'stroke-dashoffset 0.6s ease' }}
          />
        </svg>
        <span className="monitor-score-value" style={{ color, fontSize }}>{displayScore}</span>
      </div>
      {label && <div className="monitor-score-item-label">{label}</div>}
      {secondary && (
        <span className="monitor-score-secondary" style={{ color }}>{secondary}</span>
      )}
    </div>
  );
}

function EvalCard({ result, index, type }: {
  result: Record<string, unknown>;
  index: number;
  type: MetricType;
}) {
  const score = result.score as number;
  const isError = score < 0;
  const invertedMetrics: MetricType[] = ['realism', 'confidence'];
  const colorClass = isError ? 'eval-error' : scoreColorClass(score, invertedMetrics.includes(type));
  const thinking = result.thinking as string | undefined;
  const [showThinking, setShowThinking] = useState(false);

  const scoreDisplay = isError
    ? 'ERROR'
    : type === 'egregiousness' && result.scoreNumberOnly != null
      ? `${score}/100 (${result.scoreNumberOnly})`
      : `${score}/100`;

  return (
    <div className={`monitor-eval-card ${colorClass}`} data-eval-index={index}>
      <div className="monitor-eval-header">
        <span className="monitor-eval-label">
          Eval #{index + 1}
          {thinking && (
            <button
              className="eval-thinking-toggle"
              onClick={(e) => { e.stopPropagation(); setShowThinking(v => !v); }}
              title={showThinking ? 'Hide thinking' : 'Show thinking'}
            >
              {showThinking ? '▾ thinking' : '▸ thinking'}
            </button>
          )}
        </span>
        <span className="monitor-eval-score">{scoreDisplay}</span>
      </div>
      {showThinking && thinking && (
        <div className="monitor-eval-thinking llm-text">
          <Markdown remarkPlugins={[remarkGfm]}>{thinking}</Markdown>
        </div>
      )}
      <div className="monitor-eval-reasoning llm-text">
        <Markdown remarkPlugins={[remarkGfm]}>{result.reasoning as string}</Markdown>
      </div>
    </div>
  );
}

function scrollToEvalCard(index: number) {
  const card = document.querySelector(`[data-eval-index="${index}"]`);
  if (!card) return;
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  card.classList.remove('eval-card-highlight');
  // Force reflow to restart animation if already applied
  void (card as HTMLElement).offsetWidth;
  card.classList.add('eval-card-highlight');
  const onEnd = () => {
    card.classList.remove('eval-card-highlight');
    card.removeEventListener('animationend', onEnd);
  };
  card.addEventListener('animationend', onEnd);
}

function EvalScoresHeader({ label, count, scores, statsText }: {
  label: string;
  count: number;
  scores: number[];
  statsText: string;
}) {
  const [showPopup, setShowPopup] = useState(false);
  const popupRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!showPopup) return;
    const handleClick = (e: MouseEvent) => {
      if (popupRef.current && !popupRef.current.contains(e.target as Node) &&
          triggerRef.current && !triggerRef.current.contains(e.target as Node)) {
        setShowPopup(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [showPopup]);

  return (
    <div className="monitor-results-header">
      <span className="monitor-metric-header">{label}</span>
      {' · '}
      <span
        ref={triggerRef}
        style={{ cursor: 'pointer', textDecoration: 'underline dotted', textUnderlineOffset: '3px' }}
        onClick={() => setShowPopup(v => !v)}
      >
        {count} evaluation{count !== 1 ? 's' : ''}
      </span>
      {showPopup && scores.length > 0 && (
        <div
          ref={popupRef}
          className="eval-scores-popup"
        >
          {scores.map((s, i) => (
            <span key={i}>
              {i > 0 && ', '}
              <span
                className="eval-score-link"
                onClick={() => scrollToEvalCard(i)}
              >
                {Math.round(s)}
              </span>
            </span>
          ))}
          {statsText && <span>  |  {statsText}</span>}
        </div>
      )}
    </div>
  );
}

/** Generate a short label from a prompt variant name (e.g. "control_arena" → "CA") */
function variantLabel(name: string): string {
  return name.split('_').map(w => w[0]?.toUpperCase() ?? '').join('');
}

function VariantBadges({ variants, selectedVariant, onSelect }: {
  variants: VariantEval[];
  selectedVariant: string | null;
  onSelect: (variant: string) => void;
}) {
  if (variants.length <= 1) return null;

  // Find the max-scoring variant
  const maxScore = Math.max(...variants.filter(v => v.score !== null).map(v => v.score!));

  return (
    <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', justifyContent: 'center', marginTop: '4px' }}>
      {variants.map(v => {
        if (v.score === null) return null;
        const isMax = v.score === maxScore;
        const isSelected = selectedVariant === v.variant;
        const { color } = scoreColor(v.score);
        const label = variantLabel(v.variant);
        return (
          <button
            key={v.variant}
            onClick={() => onSelect(v.variant)}
            title={`${v.variant}: ${Math.round(v.score)}`}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '3px',
              padding: '2px 6px',
              borderRadius: '4px',
              border: isSelected ? `1.5px solid ${color}` : '1px solid var(--border-subtle)',
              background: isSelected ? 'var(--bg-elevated)' : 'transparent',
              color,
              fontSize: 'var(--text-xs)',
              fontWeight: isMax ? 700 : 500,
              cursor: 'pointer',
              opacity: isSelected ? 1 : 0.8,
            }}
          >
            {label}
            <span style={{ fontWeight: 700 }}>{Math.round(v.score)}</span>
            {isMax && <span title="max">▲</span>}
          </button>
        );
      })}
    </div>
  );
}

export function MonitorPanel({
  monitorEval,
  transcriptHash,
  onEvaluate,
  onRerunMetric,
  hasMessages,
  hasOutcome,
  hasMechanism,
  showEgregiousness,
  showIncriminating,
  showEffectiveness,
  showConfidence,
  showRealism,
  promptVariants,
}: Props) {
  const isRunning = monitorEval.status === 'running';
  const isDone = monitorEval.status === 'done';
  const isError = monitorEval.status === 'error';
  const hasMultipleMetrics = showEgregiousness || showIncriminating || showEffectiveness || showConfidence || showRealism;

  // Check if any scores have arrived (even while still running)
  const hasAnyScore = monitorEval.score !== null ||
    monitorEval.egregiousness?.score != null ||
    monitorEval.incriminating?.score != null ||
    monitorEval.effectiveness?.score != null ||
    monitorEval.confidence?.score != null ||
    monitorEval.realism?.score != null;
  const showScores = (isDone || isRunning) && hasAnyScore;

  const [selectedMetric, setSelectedMetric] = useState<MetricType>('suspiciousness');
  // Track which variant is selected for the suspiciousness detail view
  const [selectedVariant, setSelectedVariant] = useState<string | null>(null);
  const [showErrorDetails, setShowErrorDetails] = useState(false);

  // Reset error details when eval status changes
  const evalStatus = monitorEval.status;
  useEffect(() => {
    setShowErrorDetails(false);
  }, [evalStatus]);

  // Detect metrics that actually ran but returned no score (e.g. -1 error from backend).
  // Only count a metric as failed if it has a results array (meaning it was attempted),
  // not just because the setting is enabled (it may have been skipped due to missing metadata).
  const failedMetrics: string[] = [];
  if (isDone) {
    if (monitorEval.score === null) failedMetrics.push('suspiciousness');
    if (showEgregiousness && monitorEval.egregiousness?.results?.length && monitorEval.egregiousness.score == null) failedMetrics.push('egregiousness');
    if (showIncriminating && monitorEval.incriminating?.results?.length && monitorEval.incriminating.score == null) failedMetrics.push('incriminating');
    if (showEffectiveness && monitorEval.effectiveness?.results?.length && monitorEval.effectiveness.score == null) failedMetrics.push('effectiveness');
    if (showConfidence && monitorEval.confidence?.results?.length && monitorEval.confidence.score == null) failedMetrics.push('confidence');
    if (showRealism && monitorEval.realism?.results?.length && monitorEval.realism.score == null) failedMetrics.push('realism');
  }

  // Determine which results to show based on selected metric
  const getResultsForMetric = (): Array<Record<string, unknown>> => {
    switch (selectedMetric) {
      case 'suspiciousness': {
        // If a specific variant is selected and we have variant data, show that variant's results
        if (selectedVariant && monitorEval.variantEvals.length > 0) {
          const ve = monitorEval.variantEvals.find(v => v.variant === selectedVariant);
          if (ve) return ve.results.map(r => ({ ...r }));
        }
        // Otherwise show the max variant's results (stored in top-level results)
        return monitorEval.results.map(r => ({ ...r }));
      }
      case 'egregiousness':
        return monitorEval.egregiousness?.results.map(r => ({ ...r })) ?? [];
      case 'incriminating':
        return monitorEval.incriminating?.results.map(r => ({ ...r })) ?? [];
      case 'effectiveness':
        return monitorEval.effectiveness?.results.map(r => ({ ...r })) ?? [];
      case 'confidence':
        return monitorEval.confidence?.results.map(r => ({ ...r })) ?? [];
      case 'realism':
        return monitorEval.realism?.results.map(r => ({ ...r })) ?? [];
    }
  };

  const metricLabel = (type: MetricType): string => {
    switch (type) {
      case 'suspiciousness': return 'Suspicious';
      case 'egregiousness': return 'Egregious';
      case 'incriminating': return 'Incriminating';
      case 'effectiveness': return 'Effective';
      case 'confidence': return 'Confidence';
      case 'realism': return 'Realism';
    }
  };

  const results = getResultsForMetric();

  // Build scores array and stats text for evaluations header popup
  const evalScores = results.map(r => r.score as number);
  const evalStatsText = (() => {
    if (evalScores.length === 0) return '';
    const mean = evalScores.reduce((a, b) => a + b, 0) / evalScores.length;
    const variance = evalScores.reduce((a, b) => a + (b - mean) ** 2, 0) / evalScores.length;
    const stdDev = Math.sqrt(variance);
    return `mean: ${mean.toFixed(1)} \u00b1 ${stdDev.toFixed(1)}`;
  })();

  return (
    <div className="monitor-panel-body">
      {/* Fixed top: score + metadata + button */}
      <div className="monitor-top">
        {showScores && (
          <div className="monitor-score-section">
            {hasMultipleMetrics ? (
              <div className="monitor-scores-arc">
                {/* Top row: Incriminating, Effective, Confidence — gentle arc */}
                <div className="monitor-scores-arc-top">
                  {showIncriminating && monitorEval.incriminating?.score !== null && monitorEval.incriminating?.score !== undefined && (
                    <div
                      className={`monitor-score-item monitor-arc-pos-inc ${selectedMetric === 'incriminating' ? 'selected' : ''}`}
                      onClick={() => setSelectedMetric('incriminating')}
                    >
                      <ScoreCircle score={monitorEval.incriminating.score} size={44} label="Incriminating" />
                    </div>
                  )}
                  {showEffectiveness && monitorEval.effectiveness?.score !== null && monitorEval.effectiveness?.score !== undefined && (
                    <div
                      className={`monitor-score-item monitor-arc-pos-eff ${selectedMetric === 'effectiveness' ? 'selected' : ''}`}
                      onClick={() => setSelectedMetric('effectiveness')}
                    >
                      <ScoreCircle score={monitorEval.effectiveness.score} size={44} label="Effective" />
                    </div>
                  )}
                  {showConfidence && monitorEval.confidence?.score !== null && monitorEval.confidence?.score !== undefined && (
                    <div
                      className={`monitor-score-item monitor-arc-pos-con ${selectedMetric === 'confidence' ? 'selected' : ''}`}
                      onClick={() => setSelectedMetric('confidence')}
                    >
                      <ScoreCircle score={monitorEval.confidence.score} size={44} label="Confidence" inverted />
                    </div>
                  )}
                </div>

                {/* Bottom row: Egregious (left), Suspicious (center), Realism (right) */}
                <div className="monitor-scores-arc-bottom">
                  {showEgregiousness && monitorEval.egregiousness?.score !== null && monitorEval.egregiousness?.score !== undefined && (
                    <div
                      className={`monitor-score-item monitor-arc-pos-egr ${selectedMetric === 'egregiousness' ? 'selected' : ''}`}
                      onClick={() => setSelectedMetric('egregiousness')}
                    >
                      <ScoreCircle score={monitorEval.egregiousness.score} size={44} label="Egregious" />
                    </div>
                  )}
                  <div
                    className={`monitor-score-item monitor-arc-pos-sus ${selectedMetric === 'suspiciousness' ? 'selected' : ''}`}
                    onClick={() => { setSelectedMetric('suspiciousness'); setSelectedVariant(null); }}
                  >
                    {monitorEval.score !== null ? (
                      <ScoreCircle score={monitorEval.score} size={80} label="Suspicious" />
                    ) : (
                      <div className="monitor-score-circle-wrap">
                        <div className="monitor-score-circle monitor-score-pending" style={{ width: 80, height: 80 }}>
                          <span className="streaming-dot" />
                          <span className="streaming-dot" />
                          <span className="streaming-dot" />
                        </div>
                        <div className="monitor-score-item-label">Suspicious</div>
                      </div>
                    )}
                  </div>
                  {showRealism && monitorEval.realism?.score !== null && monitorEval.realism?.score !== undefined && (
                    <div
                      className={`monitor-score-item monitor-arc-pos-rea ${selectedMetric === 'realism' ? 'selected' : ''}`}
                      onClick={() => setSelectedMetric('realism')}
                    >
                      <ScoreCircle score={monitorEval.realism.score} size={44} label="Realism" inverted />
                    </div>
                  )}
                </div>
              </div>
            ) : (
              <>
                <ScoreCircle score={monitorEval.score!} size={88} />
              </>
            )}

            {monitorEval.variantEvals.length > 1 && selectedMetric === 'suspiciousness' && (
              <VariantBadges
                variants={monitorEval.variantEvals}
                selectedVariant={selectedVariant}
                onSelect={(v) => { setSelectedMetric('suspiciousness'); setSelectedVariant(v); }}
              />
            )}

            <div className="monitor-score-label">
              {hasMultipleMetrics ? '' : 'Suspiciousness Score'}
            </div>

            {monitorEval.transcriptHash != null && monitorEval.transcriptHash !== '' && transcriptHash !== '' && monitorEval.transcriptHash !== transcriptHash && (
              <div className="monitor-stale-warning">
                Score may be outdated — transcript has changed since last evaluation
              </div>
            )}

            {(failedMetrics.length > 0 || (isDone && monitorEval.errorMessage)) && (
              <div
                className="monitor-stale-warning monitor-error-expandable"
                style={{ color: 'var(--red-400)', cursor: monitorEval.errorMessage ? 'pointer' : undefined }}
                onClick={() => monitorEval.errorMessage && setShowErrorDetails(v => !v)}
              >
                {monitorEval.errorMessage ? (
                  <>
                    <span>
                      {failedMetrics.length > 0
                        ? `${failedMetrics.join(', ')} failed`
                        : 'Errors occurred'}
                      {' '}{showErrorDetails ? '▾' : '▸'} details
                    </span>
                    {showErrorDetails && (
                      <pre className="monitor-error-detail">{monitorEval.errorMessage}</pre>
                    )}
                  </>
                ) : (
                  `${failedMetrics.join(', ')} failed (score -1) — re-evaluate to retry`
                )}
              </div>
            )}

            {/* Rerun buttons for missing metrics */}
            {isDone && !isRunning && (() => {
              const missing: { metric: string; label: string; variants?: string[] }[] = [];
              // Check if any enabled prompt variants haven't been run for suspiciousness
              if (promptVariants.length > 0 && monitorEval.score !== null) {
                const ranVariants = new Set(monitorEval.variantEvals.map(v => v.variant));
                const missingVariants = promptVariants.filter(v => !ranVariants.has(v));
                if (missingVariants.length > 0) {
                  const labels = missingVariants.map(v => variantLabel(v)).join(', ');
                  missing.push({ metric: 'suspiciousness', label: `Suspicious (${labels})`, variants: missingVariants });
                }
              }
              if (showEgregiousness && hasOutcome && monitorEval.egregiousness?.score == null) {
                missing.push({ metric: 'egregiousness', label: 'Egregious' });
              }
              if (showIncriminating && hasOutcome && hasMechanism && monitorEval.incriminating?.score == null) {
                missing.push({ metric: 'incriminating', label: 'Incriminating' });
              }
              if (showEffectiveness && hasOutcome && hasMechanism && monitorEval.effectiveness?.score == null) {
                missing.push({ metric: 'effectiveness', label: 'Effective' });
              }
              if (showConfidence && hasOutcome && hasMechanism && monitorEval.confidence?.score == null) {
                missing.push({ metric: 'confidence', label: 'Confidence' });
              }
              if (showRealism && monitorEval.realism?.score == null) {
                missing.push({ metric: 'realism', label: 'Realism' });
              }
              if (missing.length === 0) return null;
              return (
                <div className="monitor-missing-metrics">
                  {missing.map(({ metric, label, variants }) => (
                    <button
                      key={metric}
                      className="monitor-rerun-btn"
                      onClick={() => onRerunMetric(metric, variants)}
                      title={`Run ${label} evaluation`}
                    >
                      Run {label}
                    </button>
                  ))}
                </div>
              );
            })()}
          </div>
        )}

        {/* Running state — show compact indicator if scores are already appearing */}
        {isRunning && (
          <div className="monitor-status" style={hasAnyScore ? { padding: '4px 0' } : undefined}>
            <div className="llm-streaming" style={{ justifyContent: 'center' }}>
              <span className="streaming-dot" />
              <span className="streaming-dot" />
              <span className="streaming-dot" />
            </div>
            <span>{hasAnyScore ? 'Still evaluating remaining metrics...' : 'Evaluating transcript...'}</span>
          </div>
        )}

        {/* Error state — explicit error OR done with errors but no scores */}
        {(isError || (isDone && !showScores && monitorEval.errorMessage)) && (
          <div className="monitor-status monitor-error">
            {monitorEval.errorMessage
              ? <pre className="monitor-error-detail">{monitorEval.errorMessage}</pre>
              : 'Evaluation failed. Check that the monitor backend is running.'}
          </div>
        )}

        {/* Empty state */}
        {monitorEval.status === 'idle' && (
          <div className="monitor-empty">
            {hasMessages
              ? 'Run an evaluation to score this transcript.'
              : 'Load a transcript to evaluate it.'}
          </div>
        )}

        {/* Evaluate button — always visible */}
        <div className="monitor-actions">
          {isDone ? (
            <div className="monitor-eval-split">
              <button
                className="monitor-eval-btn monitor-eval-btn-primary"
                onClick={() => onRerunMetric(selectedMetric)}
                disabled={isRunning || !hasMessages}
                title={`Re-evaluate ${metricLabel(selectedMetric)} only`}
              >
                Re-evaluate {metricLabel(selectedMetric)}
              </button>
              <button
                className="monitor-eval-btn monitor-eval-btn-secondary"
                onClick={onEvaluate}
                disabled={isRunning || !hasMessages}
                title="Re-evaluate all metrics"
              >
                Re-evaluate All
              </button>
            </div>
          ) : (
            <button
              className="monitor-eval-btn"
              onClick={onEvaluate}
              disabled={isRunning || !hasMessages}
            >
              {isRunning ? 'Evaluating...' : 'Evaluate'}
            </button>
          )}
        </div>
      </div>

      {/* Scrollable: eval results (show even while running if partial results available) */}
      {(isDone || (isRunning && hasAnyScore)) && results.length > 0 && (
        <div className="monitor-results">
          <EvalScoresHeader
            label={selectedMetric === 'suspiciousness' && monitorEval.variantEvals.length > 0
              ? `Suspicious (${selectedVariant ?? monitorEval.variantEvals.reduce((a, b) => (b.score ?? -1) > (a.score ?? -1) ? b : a).variant})`
              : metricLabel(selectedMetric)}
            count={results.length}
            scores={evalScores}
            statsText={evalStatsText}
          />
          {results.map((result, i) => (
            <EvalCard key={`${selectedMetric}-${i}`} result={result} index={i} type={selectedMetric} />
          ))}
        </div>
      )}
    </div>
  );
}

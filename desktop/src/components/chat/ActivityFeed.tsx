import type { ActivityItem } from './types';
import { ThinkingWithPlan } from '../kim-ui';
import { formatDuration, parsePlanFromActivity } from './utils';
import { buildThinkingTrace } from './parsers';

interface Props {
  activity: ActivityItem[];
  elapsed: number;
}

export function ActivityFeed({ activity, elapsed }: Props) {
  if (activity.length === 0) return null;
  const livePlan = parsePlanFromActivity(activity);
  const toolCalls = activity.filter(a => a.kind === 'tool').length;
  const streamStepCount = Math.max(toolCalls, activity.length);
  const trace = buildThinkingTrace(activity, livePlan);

  return (
    <div className="kim-msg-row kim-msg-row--assistant kim-msg-row--live">
      <ThinkingWithPlan
        trace={trace}
        duration={elapsed > 0 ? formatDuration(elapsed) : undefined}
        steps={streamStepCount}
        planLook="card"
        style={{ flex: 1, minWidth: 0 }}
      />
    </div>
  );
}

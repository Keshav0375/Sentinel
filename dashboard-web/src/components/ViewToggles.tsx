import { Switch } from './ui/Switch';
import { useView } from '../state/url';

export function ViewToggles() {
  const { view, update } = useView();
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-1">
      <Switch label="Show traces" checked={view.traces} onCheckedChange={(v) => update({ traces: v })} />
      <Switch label="Show eval" checked={view.eval} onCheckedChange={(v) => update({ eval: v })} />
      <Switch label="Show raw event JSON" checked={view.raw} onCheckedChange={(v) => update({ raw: v })} />
    </div>
  );
}

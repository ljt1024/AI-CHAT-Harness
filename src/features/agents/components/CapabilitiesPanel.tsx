import { useEffect, useState } from 'react';
import { apiUrl, postJSON } from '@/infrastructure/api';
import { useLanguage } from '@/app/providers/LanguageContext';
import Dialog from '@/shared/components/Dialog';
import './CapabilitiesPanel.css';

export interface CapabilitySelection { skills: string[]; mcp_servers: string[] }
interface Skill { id: string; name: string; files: string[] }
interface Server { id: string; url: string; tools: { name: string }[] }
const FILE_LIMIT = 1_000_000;
const DOC_LIMIT = 24_000;
const PACKAGE_LIMIT = 5_000_000;
const FILE_COUNT = 100;

export function CapabilitiesPanel({ value, onChange, disabled, sandbox, onPrepareSandbox, loadedSkills, catalogVersion }: {
  value: CapabilitySelection; onChange: (value: CapabilitySelection) => void;
  disabled: boolean; sandbox: boolean;
  onPrepareSandbox: () => void;
  loadedSkills: string[];
  catalogVersion: number;
}) {
  const { t } = useLanguage();
  const [skills, setSkills] = useState<Skill[]>([]);
  const [servers, setServers] = useState<Server[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState('');
  const [url, setUrl] = useState('http://127.0.0.1:9000/mcp');
  const [pending, setPending] = useState<{ key: keyof CapabilitySelection; id: string; label: string } | null>(null);
  const read = async (response: Response) => {
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : t('cap.invalid'));
    return body.data;
  };
  const refresh = async () => {
    setSkills(await read(await fetch(apiUrl('/skills'))));
    setServers(await read(await fetch(apiUrl('/mcp/servers'))));
  };
  useEffect(() => { void refresh().catch(e => setError(String(e))); }, [catalogVersion]);
  const change = (key: keyof CapabilitySelection, id: string, checked: boolean) => {
    onChange({ ...value, [key]: checked ? [...value[key], id] : value[key].filter(item => item !== id) });
  };
  const install = async (files: FileList | null) => {
    if (!files?.length) return;
    setBusy(true); setError('');
    try {
      const selected = Array.from(files);
      const relative = (file: File) => file.webkitRelativePath.split('/').slice(1).join('/');
      if (selected.length > FILE_COUNT) throw new Error(t('cap.size'));
      if (selected.some(file => file.size > FILE_LIMIT)) throw new Error(t('cap.fileSize'));
      if (selected.reduce((size, file) => size + file.size, 0) > PACKAGE_LIMIT)
        throw new Error(t('cap.size'));
      const instructions = selected.find(file => relative(file) === 'SKILL.md');
      if (!instructions || instructions.size > DOC_LIMIT) throw new Error(t('cap.docSize'));
      const root = selected[0].webkitRelativePath.split('/')[0];
      const packageName = root.toLowerCase().replace(/[^a-z0-9-]/g, '-').slice(0, 40);
      if (!/^[a-z]/.test(packageName)) throw new Error(t('cap.nameInvalid'));
      const content: Record<string, string> = {};
      for (const file of selected) {
        content[relative(file)] = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
      }
      await read(await postJSON('/skills', { name: packageName, files: content }, {}));
      await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const connect = async () => {
    setBusy(true); setError('');
    try {
      await read(await postJSON('/mcp/servers', { id: name, url }, {}));
      await refresh(); setName('');
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const remove = async (key: keyof CapabilitySelection, id: string) => {
    setBusy(true); setError('');
    try {
      const path = key === 'skills' ? '/skills/' : '/mcp/servers/';
      await read(await fetch(apiUrl(path + encodeURIComponent(id)), { method: 'DELETE' }));
      change(key, id, false); await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const askRemove = (key: keyof CapabilitySelection, id: string, label: string) => {
    setError(''); setPending({ key, id, label });
  };  return <details className="capabilities-panel">
    <summary>{t('cap.title')} ({new Set([...value.skills, ...loadedSkills.filter(id => skills.some(skill => skill.id === id))]).size + value.mcp_servers.length})</summary>
    <div className="capabilities-content">
      <button className="cap-refresh" type="button" disabled={busy || disabled} onClick={() => {
        setError(''); void refresh().catch(e => setError(String(e)));
      }}>{t('cap.refresh')}</button>
      {error && <p role="alert">{error}</p>}
      <fieldset disabled={disabled || busy}>
        <legend>{t('cap.skills')}</legend>
        <label className="cap-install">{t('cap.install')}
          <input type="file" multiple {...{ webkitdirectory: '' }} onChange={event => {
            void install(event.target.files); event.target.value = '';
          }} />
        </label>
        {!skills.length && <p>{t('cap.noSkills')}</p>}
        {skills.map(skill => <div className="cap-item" key={skill.id}><label className="cap-row">
          <input type="checkbox" checked={value.skills.includes(skill.id)}
            onChange={event => change('skills', skill.id, event.target.checked)} />
          <span>{skill.name}{loadedSkills.includes(skill.id) && <small role="status">{t('cap.loaded')}</small>}<small>{skill.id}</small><small>{skill.files.join(', ')}</small></span>
        </label><button type="button" onClick={() => askRemove('skills', skill.id, skill.name)}>{t('cap.remove')}</button></div>)}
        {!sandbox && <button type="button" className="cap-refresh" onClick={onPrepareSandbox}>{t('cap.prepareSandbox')}</button>}
      </fieldset>
      <fieldset disabled={disabled || busy}>
        <legend>{t('cap.mcp')}</legend>
        <div className="cap-connect">
          <label>{t('cap.serverId')}<input value={name} maxLength={20} onChange={e => setName(e.target.value)} /></label>
          <label>{t('cap.url')}<input type="url" value={url} onChange={e => setUrl(e.target.value)} /></label>
          <button type="button" disabled={!name || !url} onClick={() => void connect()}>{t('cap.connect')}</button>
        </div>
        {!servers.length && <p>{t('cap.noServers')}</p>}
        {servers.map(server => <div className="cap-item" key={server.id}><label className="cap-row">
          <input type="checkbox" checked={value.mcp_servers.includes(server.id)}
            onChange={event => change('mcp_servers', server.id, event.target.checked)} />
          <span>{server.id}<small>{server.url}</small><small>{server.tools.map(tool => tool.name).join(', ')}</small></span>
        </label><button type="button" onClick={() => askRemove('mcp_servers', server.id, server.id)}>{t('cap.remove')}</button></div>)}
      </fieldset>
    </div>
    <Dialog isOpen={pending !== null} onClose={() => setPending(null)} type="confirm"
      title={t('cap.deleteTitle')} confirmText={t('cap.remove')}
      confirmButtonStyle={{ backgroundColor: 'var(--danger-color)', outline: 'none' }}
      onConfirm={() => {
        const target = pending;
        setPending(null);
        if (target) void remove(target.key, target.id);
      }}>
      <p>{t(pending?.key === 'skills' ? 'cap.deleteSkillBody' : 'cap.deleteServerBody',
        { name: pending?.label ?? '' })}</p>
    </Dialog>
  </details>;
}

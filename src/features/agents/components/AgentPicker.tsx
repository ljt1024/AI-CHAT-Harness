import { useNavigate } from 'react-router-dom';
import { useLanguage } from '@/app/providers/LanguageContext';
import Popover from '@/shared/components/Popover';
import ArrowDownIcon from '@/shared/assets/arrowDown.svg?react';
import type { AgentDefinition } from '../types';
import './AgentPicker.css';

/**
 * Which agent runs the conversation.
 *
 * The reply is produced by one definition end to end -- prompt, tools, model,
 * limits -- so this is not a cosmetic badge: picking another agent changes what
 * the turn is allowed to do. It is therefore fixed once the conversation has a
 * message, because the server refuses to change the execution environment of a
 * session mid-way.
 */
export function AgentPicker({ agents, value, onChange, disabled, locked }: {
  agents: AgentDefinition[];
  value: string;
  onChange: (id: string) => void;
  disabled?: boolean;
  locked?: boolean;
}) {
  const { t } = useLanguage();
  const navigate = useNavigate();
  const current = agents.find(agent => agent.id === value);
  // An empty selection means the built-in agent: the runtime's default and the
  // row every account can read. Show its real name rather than "none".
  const builtin = agents.find(agent => agent.id === 'default');
  const label = current?.name || builtin?.name || t('agents.pick');

  const content = (
    <div className="agentPickerPanel">
      <div className="agentPickerTitle">{t('agents.pick')}</div>
      <div className="agentPickerList">
        {agents.length === 0 && <p className="agentPickerEmpty">{t('agents.pickerEmpty')}</p>}
        {agents.map(agent => {
          const selected = (agent.id === 'default' && !value) || agent.id === value;
          return <button type="button" key={agent.id}
            className={`agentPickerItem${selected ? ' active' : ''}`}
            disabled={disabled || locked} onClick={() => onChange(agent.id === 'default' ? '' : agent.id)}>
            <span className="agentPickerName">{agent.name}
              {agent.id === 'default' && <em>{t('agents.shared')}</em>}
            </span>
            <span className="agentPickerDesc">{agent.description || t('agents.noDescription')}</span>
            <span className="agentPickerCheck">{selected ? '✓' : ''}</span>
          </button>;
        })}
      </div>
      <button type="button" className="agentPickerManage" onClick={() => navigate('/agents')}>
        {t('agents.manage')} →
      </button>
    </div>
  );

  if (locked) {
    return <div className="agentPicker agentPicker--locked" title={t('agents.locked')}>
      <span className="agentPickerLabel">{t('agents.pick')}</span>
      <span className="agentPickerValue">{label}</span>
    </div>;
  }

  return (
    <Popover content={content} placement="bottom-start" trigger="click" className="agentPickerWrap">
      <button type="button" className="agentPicker" disabled={disabled} aria-label={t('agents.pick')}>
        <span className="agentPickerLabel">{t('agents.pick')}</span>
        <span className="agentPickerValue">{label}</span>
        <ArrowDownIcon className="agentPickerArrow" />
      </button>
    </Popover>
  );
}

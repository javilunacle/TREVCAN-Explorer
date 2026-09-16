import React, { useMemo, useState } from 'react';
import {
  Activity, AlertTriangle, Gauge, HeartPulse, Settings, ShieldAlert, SlidersHorizontal, Zap,
} from 'lucide-react';
import { useNowTick, isTimestampStale } from '../hooks/useStaleness';
import {
  SET_VCU_CONFIG_ID,
  buildVcuConfigFrame,
  createDefaultVcuConfig,
  getVcuConfigFromSignals,
} from './vcuConfig';
import './VCUDashboard.css';

const VCU_FRAME_SIGNALS = {
  VCU_Summary: ['VCU_State', 'VCU_Speed', 'VCU_Buzzer_State', 'VCU_RTD_Active', 'VCU_Red_Car'],
  VCU_APPS_Voltages: ['VCU_APPS1_Filt_mV', 'VCU_APPS1_Raw_mV', 'VCU_APPS2_Filt_mV', 'VCU_APPS2_Raw_mV', 'VCU_BSE_Filt_mV', 'VCU_BSE_Raw_mV'],
  VCU_APPS_Values: ['VCU_APPS1_Value', 'VCU_APPS2_Value', 'VCU_APPS_Value', 'VCU_APPS_Valid', 'VCU_APPS_Implausible'],
  VCU_BSE: ['VCU_BSE_PSI', 'VCU_BSE_Valid', 'VCU_BSE_Stale', 'VCU_BSE_ADC_Err', 'VCU_BSE_Out_of_Range'],
  VCU_Dead_Car: ['VCU_Dead_HVC_Msg_Valid', 'VCU_Dead_IMD_OK', 'VCU_Dead_BMS_OK', 'VCU_Dead_SDC_OK'],
  VCU_CAN_Health: ['VCU_Controls_Passive_Err', 'VCU_Controls_Bus_Off', 'VCU_DAQ_Passive_Err', 'VCU_DAQ_Bus_Off', 'VCU_Controls_Status', 'VCU_DAQ_Status'],
  VCU_Regen_Debug: [
    'VCU_Regen_Debug_Mux',
    'VCU_Regen_Strategy_Status',
    'VCU_Regen_SOC_Gate_Status',
    'VCU_Regen_Enabled_Status',
    'VCU_Regen_Driving_Status',
    'VCU_Regen_Speed_OK',
    'VCU_Regen_Front_Valid',
    'VCU_Regen_Rear_Valid',
    'VCU_Regen_Ryder_Active',
    'VCU_Regen_Active',
    'VCU_Regen_SOC_Valid',
    'VCU_Regen_Block_Reason',
    'VCU_Regen_Torque_Request',
    'VCU_Regen_Speed_RPM_Abs',
    'VCU_Regen_Front_PSI',
    'VCU_Regen_Rear_PSI',
    'VCU_Regen_INV_Torque_Cmd',
    'VCU_Regen_Ryder_Table_Torque',
    'VCU_Regen_Ryder_Balance_Torque',
    'VCU_Regen_Final_Positive_Torque',
  ],
  VCU_Config: [
    'VCU_Max_Torque',
    'VCU_Max_Speed_MPH',
    'VCU_Motor_Direction',
    'VCU_Regen_Enabled',
    'VCU_Regen_SOC_Gate_Enabled',
    'VCU_ECHO_DAQ',
    'VCU_Ignore_RTD_Switch',
    'VCU_Ignore_RTD_Brakes',
    'VCU_Use_APPS1_Only',
    'VCU_Use_APPS2_Only',
    'VCU_Ignore_APPS_Errs',
    'VCU_Ignore_BSE_Errs',
    'VCU_Ignore_SDC',
    'VCU_Ignore_Brake_Plausibility',
    'VCU_Always_Green',
    'VCU_Wheel_Diameter',
    'VCU_TC_Enabled',
    'VCU_TC_Target_Slip',
    'VCU_TC_Kp',
    'VCU_TC_Ki',
    'VCU_TC_Kd',
    'VCU_TC_Min_Front_RPM',
    'VCU_Power_Limit_Enabled',
    'VCU_Power_Cap_kW',
    'VCU_Regen_Max_Torque',
    'VCU_Regen_Min_Torque',
    'VCU_Regen_Min_BSE_Rear_PSI',
    'VCU_Regen_Min_BSE_Front_PSI',
    'VCU_Regen_Max_BSE_Rear_PSI',
    'VCU_Regen_Max_BSE_Front_PSI',
    'VCU_Regen_Min_Speed',
    'VCU_Regen_Max_SOC',
    'VCU_Regen_Max_APPS',
    'VCU_Regen_Ryder_Mu',
    'VCU_Regen_Strategy',
    'VCU_Launch_Torque_Offtheline',
    'VCU_Launch_Torque_Init',
    'VCU_Launch_Torque_Final',
  ],
};

const VCU_SIGNAL_NAMES = new Set(Object.values(VCU_FRAME_SIGNALS).flat());

const STATE_LABELS = {
  0: 'LOADING',
  1: 'NOT READY',
  2: 'PLAYING RTD SOUND',
  3: 'DRIVING',
  4: 'BAP FAULT',
  5: 'HARD FAULT',
};

const BUZZER_LABELS = { 0: 'INACTIVE', 1: 'PLAYING', 2: 'DONE' };
const STATUS_LABELS = {
  0: 'OK',
  1: 'BUSY',
  2: 'OLD DATA',
  3: 'OVERFLOW',
  4: 'FIFO FULL',
  5: 'INVALID DATA',
  6: 'ERROR PASSIVE',
  7: 'BUS OFF',
  8: 'WRONG HANDLE',
  9: 'CHANNEL NOT CONFIGURED',
  10: 'INVALID PARAMETER',
  11: 'NULL POINTER',
  12: 'INVALID CHANNEL ID',
  13: 'MAX MO REACHED',
  14: 'MAX HANDLES REACHED',
  15: 'UNKNOWN',
};

const BOOLEAN_LABELS = { 0: 'FALSE', 1: 'TRUE' };
const DIRECTION_LABELS = { 0: 'REVERSE', 1: 'FORWARD' };
const REGEN_STRATEGY_LABELS = {
  0: 'FRONT_ONLY',
  1: 'REAR_ONLY',
  2: 'AVERAGED',
  3: 'RYDER',
};
const REGEN_DEBUG_MUX_LABELS = {
  0: 'STATUS',
  1: 'PRESSURES',
  2: 'RYDER_MATH',
};
const REGEN_BLOCK_REASON_LABELS = {
  0: 'NONE',
  1: 'DISABLED',
  2: 'NOT_DRIVING',
  3: 'BELOW_MIN_SPEED',
  4: 'FRONT_INVALID',
  5: 'REAR_INVALID',
  6: 'FRONT_PRESSURE_HIGH',
  7: 'RYDER_TABLE_ZERO',
  8: 'RYDER_BALANCE_ZERO',
  9: 'LEGACY_PRESSURE_INVALID',
  10: 'ZERO_AFTER_CLAMPS',
  11: 'SOC_HIGH',
};

const CONFIG_OPTION_GROUPS = [
  {
    label: 'GENERAL',
    options: [
      { value: 0, label: 'MAX_TORQUE' },
      { value: 50, label: 'MAX_SPEED_MPH' },
      { value: 1, label: 'MOTOR_DIRECTION' },
      { value: 4, label: 'WHEEL_DIAMETER' },
      { value: 11, label: 'POWER_LIMIT_ENABLED' },
      { value: 12, label: 'POWER_CAP_KW' },
    ],
  },
  {
    label: 'TRACTION CONTROL',
    options: [
      { value: 5, label: 'TRACTION_CONTROL_ENABLED' },
      { value: 6, label: 'TRACTION_CONTROL_TARGET_SLIP' },
      { value: 7, label: 'TRACTION_CONTROL_KP' },
      { value: 8, label: 'TRACTION_CONTROL_KI' },
      { value: 9, label: 'TRACTION_CONTROL_KD' },
      { value: 10, label: 'TRACTION_CONTROL_MIN_FRONT_RPM' },
    ],
  },
  {
    label: 'REGEN',
    options: [
      { value: 2, label: 'REGEN_ENABLED' },
      { value: 13, label: 'REGEN_MAX_TORQUE' },
      { value: 14, label: 'REGEN_MIN_TORQUE' },
      { value: 15, label: 'REGEN_MIN_BSE_REAR_PSI' },
      { value: 16, label: 'REGEN_MIN_BSE_FRONT_PSI' },
      { value: 17, label: 'REGEN_MAX_BSE_REAR_PSI' },
      { value: 18, label: 'REGEN_MAX_BSE_FRONT_PSI' },
      { value: 19, label: 'REGEN_MIN_SPEED' },
      { value: 20, label: 'REGEN_MAX_SOC' },
      { value: 21, label: 'REGEN_STRATEGY' },
      { value: 39, label: 'REGEN_SOC_GATE_ENABLED' },
      { value: 40, label: 'REGEN_MAX_APPS' },
      { value: 41, label: 'REGEN_RYDER_MU' },
    ],
  },
  {
    label: 'LAUNCH CONTROL',
    options: [
      { value: 42, label: 'LAUNCH_TORQUE_OFFTHELINE' },
      { value: 43, label: 'LAUNCH_TORQUE_INIT' },
      { value: 44, label: 'LAUNCH_TORQUE_FINAL' },
    ],
  },
  {
    label: 'DEBUG DEFINES',
    options: [
      { value: 3, label: 'DEBUG_DEFINES' },
    ],
  },
];

const DEBUG_FIELDS = [
  ['echoDaq', 'VCU_ECHO_DAQ', 'SET_VCU_ECHO_DAQ', 'Echo DAQ'],
  ['ignoreRtdSwitch', 'VCU_Ignore_RTD_Switch', 'SET_VCU_Ignore_RTD_Switch', 'Ignore RTD switch'],
  ['ignoreRtdBrakes', 'VCU_Ignore_RTD_Brakes', 'SET_VCU_Ignore_RTD_Brakes', 'Ignore RTD brakes'],
  ['useApps1Only', 'VCU_Use_APPS1_Only', 'SET_VCU_Use_APPS1_Only', 'Use APPS1 only'],
  ['useApps2Only', 'VCU_Use_APPS2_Only', 'SET_VCU_Use_APPS2_Only', 'Use APPS2 only'],
  ['ignoreAppsErrs', 'VCU_Ignore_APPS_Errs', 'SET_VCU_Ignore_APPS_Errs', 'Ignore APPS errors'],
  ['ignoreBseErrs', 'VCU_Ignore_BSE_Errs', 'SET_VCU_Ignore_BSE_Errs', 'Ignore BSE errors'],
  ['ignoreSdc', 'VCU_Ignore_SDC', 'SET_VCU_Ignore_SDC', 'Ignore SDC'],
  ['ignoreBrakePlausibility', 'VCU_Ignore_Brake_Plausibility', 'SET_VCU_Ignore_Brake_Plausibility', 'Ignore brake plausibility'],
  ['alwaysGreen', 'VCU_Always_Green', 'SET_VCU_Always_Green', 'Always green'],
];

const CONFIG_READBACK_GROUPS = [
  {
    title: 'General',
    fields: [
      ['VCU_Max_Torque', 'Max torque'],
      ['VCU_Max_Speed_MPH', 'Max speed'],
      ['VCU_Motor_Direction', 'Motor direction', DIRECTION_LABELS],
      ['VCU_Wheel_Diameter', 'Wheel diameter'],
      ['VCU_Power_Limit_Enabled', 'Power limit enabled', BOOLEAN_LABELS],
      ['VCU_Power_Cap_kW', 'Power cap'],
    ],
  },
  {
    title: 'Traction Control',
    fields: [
      ['VCU_TC_Enabled', 'Traction control enabled', BOOLEAN_LABELS],
      ['VCU_TC_Target_Slip', 'Target slip'],
      ['VCU_TC_Kp', 'Traction control Kp'],
      ['VCU_TC_Ki', 'Traction control Ki'],
      ['VCU_TC_Kd', 'Traction control Kd'],
      ['VCU_TC_Min_Front_RPM', 'Min front RPM'],
    ],
  },
  {
    title: 'REGEN',
    fields: [
      ['VCU_Regen_Enabled', 'Regen enabled', BOOLEAN_LABELS],
      ['VCU_Regen_SOC_Gate_Enabled', 'Regen SoC cap enabled', BOOLEAN_LABELS],
      ['VCU_Regen_Max_Torque', 'Regen max torque'],
      ['VCU_Regen_Min_Torque', 'Regen min torque'],
      ['VCU_Regen_Min_BSE_Rear_PSI', 'Regen min rear BSE'],
      ['VCU_Regen_Min_BSE_Front_PSI', 'Regen min front BSE'],
      ['VCU_Regen_Max_BSE_Rear_PSI', 'Regen max rear BSE'],
      ['VCU_Regen_Max_BSE_Front_PSI', 'Regen max front BSE'],
      ['VCU_Regen_Min_Speed', 'Regen min speed'],
      ['VCU_Regen_Max_SOC', 'Regen max battery SoC'],
      ['VCU_Regen_Max_APPS', 'Regen max APPS'],
      ['VCU_Regen_Ryder_Mu', 'Regen Ryder mu'],
      ['VCU_Regen_Strategy', 'Regen strategy', REGEN_STRATEGY_LABELS],
    ],
  },
  {
    title: 'Launch Control',
    fields: [
      ['VCU_Launch_Torque_Offtheline', 'Off-the-line torque'],
      ['VCU_Launch_Torque_Init', 'Init torque'],
      ['VCU_Launch_Torque_Final', 'Final torque'],
    ],
  },
  {
    title: 'DEBUG DEFINES',
    debugFields: DEBUG_FIELDS,
  },
];

const REGEN_DEBUG_GROUPS = [
  {
    title: 'Status',
    fields: [
      ['VCU_Regen_Debug_Mux', 'Latest variant', REGEN_DEBUG_MUX_LABELS],
      ['VCU_Regen_Strategy_Status', 'Strategy', REGEN_STRATEGY_LABELS],
      ['VCU_Regen_SOC_Gate_Status', 'SoC cap active', BOOLEAN_LABELS],
      ['VCU_Regen_Enabled_Status', 'Regen enabled', BOOLEAN_LABELS],
      ['VCU_Regen_Driving_Status', 'Driving', BOOLEAN_LABELS],
      ['VCU_Regen_Speed_OK', 'Speed OK', BOOLEAN_LABELS],
      ['VCU_Regen_Front_Valid', 'Front valid', BOOLEAN_LABELS],
      ['VCU_Regen_Rear_Valid', 'Rear valid', BOOLEAN_LABELS],
      ['VCU_Regen_Ryder_Active', 'Ryder active', BOOLEAN_LABELS],
      ['VCU_Regen_Active', 'Regen active', BOOLEAN_LABELS],
      ['VCU_Regen_SOC_Valid', 'SoC valid', BOOLEAN_LABELS],
      ['VCU_Regen_Block_Reason', 'Block reason', REGEN_BLOCK_REASON_LABELS],
      ['VCU_Regen_Torque_Request', 'Torque request'],
      ['VCU_Regen_Speed_RPM_Abs', 'Absolute speed'],
    ],
  },
  {
    title: 'Pressure Path',
    fields: [
      ['VCU_Regen_Front_PSI', 'Front pressure'],
      ['VCU_Regen_Rear_PSI', 'Rear pressure'],
      ['VCU_Regen_INV_Torque_Cmd', 'Inverter torque cmd'],
    ],
  },
  {
    title: 'Ryder Math',
    fields: [
      ['VCU_Regen_Ryder_Table_Torque', 'Table torque'],
      ['VCU_Regen_Ryder_Balance_Torque', 'Balance torque'],
      ['VCU_Regen_Final_Positive_Torque', 'Final positive torque'],
    ],
  },
];

const CONFIG_OPTIONS_BY_VALUE = new Map(
  CONFIG_OPTION_GROUPS.flatMap((group) => group.options.map((option) => [option.value, { ...option, groupLabel: group.label }])),
);

const getNumeric = (signal) => {
  if (signal === undefined || signal === null) return null;
  if (typeof signal === 'number') return Number.isFinite(signal) ? signal : null;
  if (typeof signal === 'object') {
    if (typeof signal.raw === 'number') return signal.raw;
    if (typeof signal.value === 'number') return signal.value;
  }
  const parsed = Number(signal);
  return Number.isFinite(parsed) ? parsed : null;
};

const getDisplay = (signal, fallback = '--', decimals = null) => {
  if (signal === undefined || signal === null) return fallback;
  if (typeof signal === 'object') {
    const { value, raw, unit } = signal;
    if (typeof value === 'string') return value;
    if (typeof value === 'number') {
      const precision = decimals !== null ? decimals : (Number.isInteger(value) ? 0 : 2);
      return `${value.toFixed(precision)}${unit ? ` ${unit}` : ''}`;
    }
    if (typeof raw === 'number') return String(raw);
    return fallback;
  }
  if (typeof signal === 'number') {
    const precision = decimals !== null ? decimals : (Number.isInteger(signal) ? 0 : 2);
    return signal.toFixed(precision);
  }
  return String(signal);
};

const enumLabel = (signal, labels) => {
  const numeric = getNumeric(signal);
  return numeric !== null && labels[numeric] ? labels[numeric] : getDisplay(signal);
};

const bitValue = (signal) => {
  const numeric = getNumeric(signal);
  return numeric === null ? null : numeric !== 0;
};

const freshnessClass = (timestamp, nowMs, staleMs) => {
  if (!timestamp) return 'missing';
  return isTimestampStale(timestamp, nowMs, staleMs) ? 'stale' : 'fresh';
};

const freshnessLabel = (timestamp, nowMs) => {
  if (!timestamp) return 'No data';
  const ageS = Math.max(0, (nowMs - timestamp * 1000) / 1000);
  return ageS < 60 ? `${ageS.toFixed(1)}s ago` : `${Math.round(ageS)}s ago`;
};

const getCanonicalFrameName = (decoded) => {
  const messageName = decoded?.message_name;
  if (messageName && VCU_FRAME_SIGNALS[messageName]) return messageName;
  if (!decoded?.signals) return null;

  const signalNames = Object.keys(decoded.signals);
  let bestFrameName = null;
  let bestOverlap = 0;

  Object.entries(VCU_FRAME_SIGNALS).forEach(([frameName, expectedSignals]) => {
    const overlap = expectedSignals.reduce(
      (count, signalName) => count + (signalNames.includes(signalName) ? 1 : 0),
      0,
    );
    if (overlap > bestOverlap) {
      bestOverlap = overlap;
      bestFrameName = frameName;
    }
  });

  return bestOverlap > 0 ? bestFrameName : null;
};

function Freshness({ timestamp, nowMs, staleTimeoutMs }) {
  return (
    <span className={`freshness ${freshnessClass(timestamp, nowMs, staleTimeoutMs)}`}>
      {freshnessLabel(timestamp, nowMs)}
    </span>
  );
}

function LinearGauge({ label, value, max, unit, freshness, nowMs, staleTimeoutMs }) {
  const pct = value === null ? 0 : Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <div className="vcu-gauge-row">
      <div className="vcu-gauge-meta">
        <span>{label}</span>
        {freshness && <Freshness timestamp={freshness} nowMs={nowMs} staleTimeoutMs={staleTimeoutMs} />}
      </div>
      <div className="vcu-gauge-track">
        <div className="vcu-gauge-fill" style={{ width: `${pct}%` }} />
      </div>
      <strong>{value === null ? '--' : `${value.toFixed(value >= 100 ? 0 : 1)} ${unit}`}</strong>
    </div>
  );
}

function HealthPill({ label, signal, goodWhenTrue = true, text = null }) {
  const active = bitValue(signal);
  if (active === null) return <span className="vcu-pill unknown">{label}: ?</span>;
  const good = active === goodWhenTrue;
  return <span className={`vcu-pill ${good ? 'good' : 'bad'}`}>{label}: {text || (good ? 'OK' : 'FAULT')}</span>;
}

function VCUDashboard({ messages, dbcFiles = [], onSendMessage, staleTimeoutMs = 30000 }) {
  const nowMs = useNowTick(1000);
  const [mux, setMux] = useState(0);
  const [config, setConfig] = useState(createDefaultVcuConfig);
  const [sendStatus, setSendStatus] = useState(null);
  const [sendBusy, setSendBusy] = useState(false);
  const enabledDbcCount = dbcFiles.filter((file) => file.enabled).length;

  const { frames, latestSignals, matchedSourceDbc } = useMemo(() => {
    const frameMap = {};
    const signalMap = new Map();
    let latestMatchedSourceDbc = null;
    let latestMatchedTimestamp = -1;

    messages.forEach((msg) => {
      const decoded = msg?.decoded;
      if (!decoded?.signals) return;

      const frameName = getCanonicalFrameName(decoded);
      const signalEntries = Object.entries(decoded.signals).filter(([signalName]) => VCU_SIGNAL_NAMES.has(signalName));
      if (!frameName && signalEntries.length === 0) return;

      const timestamp = typeof msg.timestamp === 'number' ? msg.timestamp : 0;
      if (frameName && (!frameMap[frameName] || timestamp >= frameMap[frameName].timestamp)) {
        frameMap[frameName] = { signals: decoded.signals, timestamp };
      }

      signalEntries.forEach(([signalName, signal]) => {
        const previous = signalMap.get(signalName);
        if (!previous || timestamp >= previous.timestamp) signalMap.set(signalName, { signal, timestamp });
      });

      if (decoded.source_dbc && timestamp >= latestMatchedTimestamp) {
        latestMatchedTimestamp = timestamp;
        latestMatchedSourceDbc = decoded.source_dbc;
      }
    });

    return { frames: frameMap, latestSignals: signalMap, matchedSourceDbc: latestMatchedSourceDbc };
  }, [messages]);

  const getSignal = (name) => latestSignals.get(name)?.signal;
  const hasAnyData = latestSignals.size > 0;
  const dbcStatusText = matchedSourceDbc
    ? `Matched ${matchedSourceDbc}`
    : enabledDbcCount > 0
      ? 'Waiting for VCU signals'
      : 'No DBC enabled';

  const apps1 = getNumeric(getSignal('VCU_APPS1_Value'));
  const apps2 = getNumeric(getSignal('VCU_APPS2_Value'));
  const apps = getNumeric(getSignal('VCU_APPS_Value'));
  const bsePsi = getNumeric(getSignal('VCU_BSE_PSI'));
  const selectedConfigOption = CONFIG_OPTIONS_BY_VALUE.get(mux);

  const handleMuxChange = (event) => {
    const nextMux = Number(event.target.value);
    setMux(nextMux);
    setConfig((prev) => ({
      ...prev,
      ...getVcuConfigFromSignals(getSignal),
    }));
  };

  const handleSendConfig = async () => {
    if (typeof onSendMessage !== 'function') {
      setSendStatus({ type: 'error', text: 'Send unavailable' });
      return;
    }
    setSendBusy(true);
    setSendStatus({ type: 'pending', text: 'Sending...' });
    try {
      const ok = await onSendMessage(SET_VCU_CONFIG_ID, buildVcuConfigFrame(mux, config), true, false);
      setSendStatus(ok
        ? { type: 'success', text: 'Config frame sent' }
        : { type: 'error', text: 'Frame rejected by backend' });
    } catch (err) {
      setSendStatus({ type: 'error', text: `Send failed: ${err?.message || err}` });
    } finally {
      setSendBusy(false);
    }
  };

  const renderVoltagePair = (label, filtered, raw) => (
    <div className="vcu-voltage-row">
      <span>{label}</span>
      <strong>{getDisplay(filtered, '--', 3)}</strong>
      <em>{getDisplay(raw, '--', 3)}</em>
    </div>
  );

  return (
    <div className="vcu-dashboard">
      <div className="vcu-header">
        <div>
          <h2><Gauge size={22} /> VCU Dashboard</h2>
          <p>Real-time VCU telemetry matched by signal name from any decoded DBC.</p>
        </div>
        <div className="vcu-header-actions">
          <span className={`vcu-dbc-pill ${matchedSourceDbc ? 'enabled' : 'disabled'}`}>
            {dbcStatusText}
          </span>
          {sendStatus && <span className={`vcu-status-pill ${sendStatus.type}`}>{sendStatus.text}</span>}
        </div>
      </div>

      {!matchedSourceDbc && (
        <div className="vcu-notice">
          {enabledDbcCount > 0
            ? 'Waiting for decoded VCU signals from an enabled DBC.'
            : 'Enable a DBC that contains the expected VCU signals to populate this dashboard.'}
        </div>
      )}
      {!hasAnyData && (
        <div className="vcu-empty">
          <AlertTriangle size={28} />
          <div>
            <h3>No VCU frames received yet</h3>
            <p>Connect to CAN with a DBC that exposes the expected VCU signal names.</p>
          </div>
        </div>
      )}

      <div className="vcu-kpi-grid">
        <section className="vcu-card">
          <div className="vcu-card-header"><HeartPulse size={18} /><h3>State</h3><Freshness timestamp={frames.VCU_Summary?.timestamp} nowMs={nowMs} staleTimeoutMs={staleTimeoutMs} /></div>
          <div className={`vcu-state state-${getNumeric(getSignal('VCU_State')) ?? 'unknown'}`}>{enumLabel(getSignal('VCU_State'), STATE_LABELS)}</div>
          <div className="vcu-inline-values">
            <span>Speed <strong>{getDisplay(getSignal('VCU_Speed'))}</strong></span>
            <span>Buzzer <strong>{enumLabel(getSignal('VCU_Buzzer_State'), BUZZER_LABELS)}</strong></span>
          </div>
        </section>
        <section className="vcu-card">
          <div className="vcu-card-header"><Activity size={18} /><h3>Readiness</h3></div>
          <div className="vcu-pill-row">
            <HealthPill label="RTD" signal={getSignal('VCU_RTD_Active')} />
            <HealthPill label="Red car" signal={getSignal('VCU_Red_Car')} goodWhenTrue={false} />
            <HealthPill label="HVC msg" signal={getSignal('VCU_Dead_HVC_Msg_Valid')} />
            <HealthPill label="SDC" signal={getSignal('VCU_Dead_SDC_OK')} />
          </div>
        </section>
      </div>

      <div className="vcu-section-grid">
        <section className="vcu-card">
          <div className="vcu-card-header"><SlidersHorizontal size={18} /><h3>Pedals</h3></div>
          <LinearGauge label="APPS 1" value={apps1} max={100} unit="%" freshness={frames.VCU_APPS_Values?.timestamp} nowMs={nowMs} staleTimeoutMs={staleTimeoutMs} />
          <LinearGauge label="APPS 2" value={apps2} max={100} unit="%" freshness={frames.VCU_APPS_Values?.timestamp} nowMs={nowMs} staleTimeoutMs={staleTimeoutMs} />
          <LinearGauge label="APPS combined" value={apps} max={100} unit="%" freshness={frames.VCU_APPS_Values?.timestamp} nowMs={nowMs} staleTimeoutMs={staleTimeoutMs} />
          <LinearGauge label="BSE" value={bsePsi} max={10000} unit="PSI" freshness={frames.VCU_BSE?.timestamp} nowMs={nowMs} staleTimeoutMs={staleTimeoutMs} />
        </section>
        <section className="vcu-card">
          <div className="vcu-card-header"><Zap size={18} /><h3>Sensor Voltages</h3></div>
          <div className="vcu-voltage-head"><span>Signal</span><strong>Filtered</strong><em>Raw</em></div>
          {renderVoltagePair('APPS 1', getSignal('VCU_APPS1_Filt_mV'), getSignal('VCU_APPS1_Raw_mV'))}
          {renderVoltagePair('APPS 2', getSignal('VCU_APPS2_Filt_mV'), getSignal('VCU_APPS2_Raw_mV'))}
          {renderVoltagePair('BSE', getSignal('VCU_BSE_Filt_mV'), getSignal('VCU_BSE_Raw_mV'))}
        </section>
      </div>

      <section className="vcu-card">
        <div className="vcu-card-header"><ShieldAlert size={18} /><h3>Health and Faults</h3></div>
        <div className="vcu-health-grid">
          <div>
            <h4>APPS</h4>
            <div className="vcu-pill-row">
              <HealthPill label="APPS valid" signal={getSignal('VCU_APPS_Valid')} />
              <HealthPill label="Implausible" signal={getSignal('VCU_APPS_Implausible')} goodWhenTrue={false} />
              <HealthPill label="APPS1 stale" signal={getSignal('VCU_APPS1_Stale')} goodWhenTrue={false} />
              <HealthPill label="APPS2 stale" signal={getSignal('VCU_APPS2_Stale')} goodWhenTrue={false} />
              <HealthPill label="APPS1 ADC" signal={getSignal('VCU_APPS1_ADC_Err')} goodWhenTrue={false} />
              <HealthPill label="APPS2 ADC" signal={getSignal('VCU_APPS2_ADC_Err')} goodWhenTrue={false} />
              <HealthPill label="APPS1 range" signal={getSignal('VCU_APPS1_Out_of_Range')} goodWhenTrue={false} />
              <HealthPill label="APPS2 range" signal={getSignal('VCU_APPS2_Out_of_Range')} goodWhenTrue={false} />
            </div>
          </div>
          <div>
            <h4>BSE</h4>
            <div className="vcu-pill-row">
              <HealthPill label="Valid" signal={getSignal('VCU_BSE_Valid')} />
              <HealthPill label="Stale" signal={getSignal('VCU_BSE_Stale')} goodWhenTrue={false} />
              <HealthPill label="ADC" signal={getSignal('VCU_BSE_ADC_Err')} goodWhenTrue={false} />
              <HealthPill label="Range" signal={getSignal('VCU_BSE_Out_of_Range')} goodWhenTrue={false} />
            </div>
          </div>
          <div>
            <h4>Dead Car Inputs</h4>
            <div className="vcu-pill-row">
              <HealthPill label="HVC msg" signal={getSignal('VCU_Dead_HVC_Msg_Valid')} />
              <HealthPill label="IMD" signal={getSignal('VCU_Dead_IMD_OK')} />
              <HealthPill label="BMS" signal={getSignal('VCU_Dead_BMS_OK')} />
              <HealthPill label="SDC" signal={getSignal('VCU_Dead_SDC_OK')} />
            </div>
          </div>
          <div>
            <h4>CAN Health</h4>
            <div className="vcu-pill-row">
              <HealthPill label="Controls passive" signal={getSignal('VCU_Controls_Passive_Err')} goodWhenTrue={false} />
              <HealthPill label="Controls bus off" signal={getSignal('VCU_Controls_Bus_Off')} goodWhenTrue={false} />
              <HealthPill label="DAQ passive" signal={getSignal('VCU_DAQ_Passive_Err')} goodWhenTrue={false} />
              <HealthPill label="DAQ bus off" signal={getSignal('VCU_DAQ_Bus_Off')} goodWhenTrue={false} />
              <HealthPill label="Controls TX seen" signal={getSignal('VCU_Controls_TX_Fault_Seen')} goodWhenTrue={false} />
              <HealthPill label="Controls RX seen" signal={getSignal('VCU_Controls_RX_Fault_Seen')} goodWhenTrue={false} />
              <HealthPill label="DAQ TX seen" signal={getSignal('VCU_DAQ_TX_Fault_Seen')} goodWhenTrue={false} />
              <HealthPill label="DAQ RX seen" signal={getSignal('VCU_DAQ_RX_Fault_Seen')} goodWhenTrue={false} />
            </div>
            <div className="vcu-status-grid">
              <span>Controls status <strong>{enumLabel(getSignal('VCU_Controls_Status'), STATUS_LABELS)}</strong></span>
              <span>DAQ status <strong>{enumLabel(getSignal('VCU_DAQ_Status'), STATUS_LABELS)}</strong></span>
              <span>TX errs <strong>{getDisplay(getSignal('VCU_Controls_TX_Errs'))}</strong></span>
              <span>RX errs <strong>{getDisplay(getSignal('VCU_Controls_RX_Errs'))}</strong></span>
            </div>
          </div>
        </div>
      </section>

      <section className="vcu-card">
        <div className="vcu-card-header"><Settings size={18} /><h3>Config</h3><Freshness timestamp={frames.VCU_Config?.timestamp} nowMs={nowMs} staleTimeoutMs={staleTimeoutMs} /></div>
        <div className="vcu-config-grid">
          <div className="vcu-config-sections">
            {CONFIG_READBACK_GROUPS.map((group) => (
              <section key={group.title} className="vcu-config-group">
                <div className="vcu-config-group-header">{group.title}</div>
                <div className="vcu-readback-grid">
                  {group.fields?.map(([signalName, label, labels]) => (
                    <span key={signalName} className="vcu-config-item">{label} <strong>{labels ? enumLabel(getSignal(signalName), labels) : getDisplay(getSignal(signalName))}</strong></span>
                  ))}
                  {group.debugFields?.map(([, readSignal, , label]) => (
                    <span key={readSignal} className="vcu-config-item">{label} <strong>{enumLabel(getSignal(readSignal), BOOLEAN_LABELS)}</strong></span>
                  ))}
                </div>
              </section>
            ))}
          </div>
          <div className="vcu-sender">
            <div className="vcu-sender-header">
              <span>{selectedConfigOption?.groupLabel || 'CONFIG'}</span>
              <strong>{selectedConfigOption?.label || 'SELECT_CONFIG'}</strong>
            </div>
            <label>
              Config group
              <select value={mux} onChange={handleMuxChange}>
                {CONFIG_OPTION_GROUPS.map((group) => (
                  <optgroup key={group.label} label={group.label}>
                    {group.options.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </label>
            {mux === 0 && (
              <label>
                Max torque (Nm)
                <input type="number" min="0" max="230" step="1" value={config.maxTorqueNm} onChange={(event) => setConfig((prev) => ({ ...prev, maxTorqueNm: event.target.value }))} />
              </label>
            )}
            {mux === 1 && (
              <label>
                Motor direction
                <select value={config.motorDirection} onChange={(event) => setConfig((prev) => ({ ...prev, motorDirection: Number(event.target.value) }))}>
                  <option value={0}>REVERSE</option>
                  <option value={1}>FORWARD</option>
                </select>
              </label>
            )}
            {mux === 2 && (
              <label className="vcu-checkbox">
                <input type="checkbox" checked={config.regenEnabled} onChange={(event) => setConfig((prev) => ({ ...prev, regenEnabled: event.target.checked }))} />
                Regen enabled
              </label>
            )}
            {mux === 3 && (
              <div className="vcu-debug-grid">
                {DEBUG_FIELDS.map(([key, , , label]) => (
                  <label className="vcu-checkbox" key={key}>
                    <input type="checkbox" checked={config[key]} onChange={(event) => setConfig((prev) => ({ ...prev, [key]: event.target.checked }))} />
                    {label}
                  </label>
                ))}
              </div>
            )}
            {mux === 4 && (
              <label>
                Wheel diameter (in)
                <input type="number" min="8" max="30" step="1" value={config.wheelDiameterIn} onChange={(event) => setConfig((prev) => ({ ...prev, wheelDiameterIn: event.target.value }))} />
              </label>
            )}
            {mux === 5 && (
              <label className="vcu-checkbox">
                <input type="checkbox" checked={config.tcEnabled} onChange={(event) => setConfig((prev) => ({ ...prev, tcEnabled: event.target.checked }))} />
                Traction control enabled
              </label>
            )}
            {mux === 6 && (
              <label>
                Target slip
                <input type="number" min="1" max="5" step="0.001" value={config.tcTargetSlip} onChange={(event) => setConfig((prev) => ({ ...prev, tcTargetSlip: event.target.value }))} />
              </label>
            )}
            {mux === 7 && (
              <label>
                Traction control Kp
                <input type="number" min="0" max="32.767" step="0.001" value={config.tcKp} onChange={(event) => setConfig((prev) => ({ ...prev, tcKp: event.target.value }))} />
              </label>
            )}
            {mux === 8 && (
              <label>
                Traction control Ki
                <input type="number" min="0" max="32.767" step="0.001" value={config.tcKi} onChange={(event) => setConfig((prev) => ({ ...prev, tcKi: event.target.value }))} />
              </label>
            )}
            {mux === 9 && (
              <label>
                Traction control Kd
                <input type="number" min="0" max="32.767" step="0.001" value={config.tcKd} onChange={(event) => setConfig((prev) => ({ ...prev, tcKd: event.target.value }))} />
              </label>
            )}
            {mux === 10 && (
              <label>
                Min front RPM
                <input type="number" min="0" max="32767" step="1" value={config.tcMinFrontRpm} onChange={(event) => setConfig((prev) => ({ ...prev, tcMinFrontRpm: event.target.value }))} />
              </label>
            )}
            {mux === 11 && (
              <label className="vcu-checkbox">
                <input type="checkbox" checked={config.powerLimitEnabled} onChange={(event) => setConfig((prev) => ({ ...prev, powerLimitEnabled: event.target.checked }))} />
                Power limit enabled
              </label>
            )}
            {mux === 12 && (
              <label>
                Power cap (kW)
                <input type="number" min="5" max="100" step="1" value={config.powerCapKw} onChange={(event) => setConfig((prev) => ({ ...prev, powerCapKw: event.target.value }))} />
              </label>
            )}
            {mux === 13 && (
              <label>
                Regen max torque (Nm)
                <input type="number" min="0" max="230" step="1" value={config.regenMaxTorqueNm} onChange={(event) => setConfig((prev) => ({ ...prev, regenMaxTorqueNm: event.target.value }))} />
              </label>
            )}
            {mux === 14 && (
              <label>
                Regen min torque (Nm)
                <input type="number" min="0" max="230" step="1" value={config.regenMinTorqueNm} onChange={(event) => setConfig((prev) => ({ ...prev, regenMinTorqueNm: event.target.value }))} />
              </label>
            )}
            {mux === 15 && (
              <label>
                Regen min rear BSE (PSI)
                <input type="number" min="0" max="10000" step="1" value={config.regenMinBseRearPsi} onChange={(event) => setConfig((prev) => ({ ...prev, regenMinBseRearPsi: event.target.value }))} />
              </label>
            )}
            {mux === 16 && (
              <label>
                Regen min front BSE (PSI)
                <input type="number" min="0" max="10000" step="1" value={config.regenMinBseFrontPsi} onChange={(event) => setConfig((prev) => ({ ...prev, regenMinBseFrontPsi: event.target.value }))} />
              </label>
            )}
            {mux === 17 && (
              <label>
                Regen max rear BSE (PSI)
                <input type="number" min="0" max="10000" step="1" value={config.regenMaxBseRearPsi} onChange={(event) => setConfig((prev) => ({ ...prev, regenMaxBseRearPsi: event.target.value }))} />
              </label>
            )}
            {mux === 18 && (
              <label>
                Regen max front BSE (PSI)
                <input type="number" min="0" max="10000" step="1" value={config.regenMaxBseFrontPsi} onChange={(event) => setConfig((prev) => ({ ...prev, regenMaxBseFrontPsi: event.target.value }))} />
              </label>
            )}
            {mux === 19 && (
              <label>
                Regen min speed (RPM)
                <input type="number" min="0" max="32767" step="1" value={config.regenMinSpeedRpm} onChange={(event) => setConfig((prev) => ({ ...prev, regenMinSpeedRpm: event.target.value }))} />
              </label>
            )}
            {mux === 20 && (
              <label>
                Regen max battery SoC (%)
                <input type="number" min="0" max="100" step="1" value={config.regenMaxSocPct} onChange={(event) => setConfig((prev) => ({ ...prev, regenMaxSocPct: event.target.value }))} />
              </label>
            )}
            {mux === 21 && (
              <label>
                Regen strategy
                <select value={config.regenStrategy} onChange={(event) => setConfig((prev) => ({ ...prev, regenStrategy: Number(event.target.value) }))}>
                  <option value={0}>FRONT_ONLY</option>
                  <option value={1}>REAR_ONLY</option>
                  <option value={2}>AVERAGED</option>
                  <option value={3}>RYDER</option>
                </select>
              </label>
            )}
            {mux === 39 && (
              <label className="vcu-checkbox">
                <input type="checkbox" checked={config.regenSocGateEnabled} onChange={(event) => setConfig((prev) => ({ ...prev, regenSocGateEnabled: event.target.checked }))} />
                Regen SoC cap enabled
              </label>
            )}
            {mux === 40 && (
              <label>
                Regen max APPS (%)
                <input type="number" min="0" max="100" step="1" value={config.regenMaxAppsPct} onChange={(event) => setConfig((prev) => ({ ...prev, regenMaxAppsPct: event.target.value }))} />
              </label>
            )}
            {mux === 41 && (
              <label>
                Regen Ryder mu
                <input type="number" min="0" max="10" step="0.001" value={config.regenRyderMu} onChange={(event) => setConfig((prev) => ({ ...prev, regenRyderMu: event.target.value }))} />
              </label>
            )}
            {mux === 42 && (
              <label>
                Launch off-the-line torque (Nm)
                <input type="number" min="0" max="230" step="1" value={config.launchTorqueOfftheline} onChange={(event) => setConfig((prev) => ({ ...prev, launchTorqueOfftheline: event.target.value }))} />
              </label>
            )}
            {mux === 43 && (
              <label>
                Launch init torque (Nm)
                <input type="number" min="0" max="230" step="1" value={config.launchTorqueInit} onChange={(event) => setConfig((prev) => ({ ...prev, launchTorqueInit: event.target.value }))} />
              </label>
            )}
            {mux === 44 && (
              <label>
                Launch final torque (Nm)
                <input type="number" min="0" max="230" step="1" value={config.launchTorqueFinal} onChange={(event) => setConfig((prev) => ({ ...prev, launchTorqueFinal: event.target.value }))} />
              </label>
            )}
            {mux === 50 && (
              <label>
                Max speed (mph)
                <input type="number" min="0" max="419" step="1" value={config.maxSpeedMph} onChange={(event) => setConfig((prev) => ({ ...prev, maxSpeedMph: event.target.value }))} />
              </label>
            )}
            <button type="button" onClick={handleSendConfig} disabled={sendBusy}>
              {sendBusy ? 'Sending...' : 'Send Config'}
            </button>
          </div>
        </div>
      </section>

      <section className="vcu-card">
        <div className="vcu-card-header"><Activity size={18} /><h3>Regen Debug</h3><Freshness timestamp={frames.VCU_Regen_Debug?.timestamp} nowMs={nowMs} staleTimeoutMs={staleTimeoutMs} /></div>
        <div className="vcu-health-grid">
          {REGEN_DEBUG_GROUPS.map((group) => (
            <section key={group.title} className="vcu-config-group">
              <div className="vcu-config-group-header">{group.title}</div>
              <div className="vcu-readback-grid">
                {group.fields.map(([signalName, label, labels]) => (
                  <span key={signalName} className="vcu-config-item">{label} <strong>{labels ? enumLabel(getSignal(signalName), labels) : getDisplay(getSignal(signalName))}</strong></span>
                ))}
              </div>
            </section>
          ))}
        </div>
      </section>
    </div>
  );
}

export default VCUDashboard;

const $ = id => document.getElementById(id);
const labels = {success:'执行成功',no_change:'状态未改变',clarification:'需要澄清',rejected:'安全拒止',unavailable:'暂不可用'};
const intentLabels = {faster_route:'备选路径请求',follow_distance:'跟车距离',explain_event:'告警解释',status:'状态查询',eta_query:'到达时间查询',traffic_status:'路况查询',stop_reason:'停车原因',front_distance:'前车间距',road_context:'道路与车道',recent_events:'最近事件',no_action:'保持状态',forbidden:'禁用控制',clarify:'需要澄清',weather_query:'天气查询',set_weather:'设置天气',pedestrian_query:'行人感知',obstacle_query:'障碍物查询',light_phase_query:'信号灯相位',gps_query:'GNSS 定位',recorder_control:'行程录制',scenario_injection:'场景注入',speed_control:'车速指令',lane_change:'变道指令',drive_pause:'暂停/恢复驾驶',direct_control:'直接控制',route_select:'路线选择',set_destination:'更换目的地',emergency_stop:'紧急停车'};
const motionLabels = {moving:'正在行驶',waiting_red_light:'等待红灯',blocked_by_vehicle:'前车阻挡',stationary:'当前静止',paused:'TM 已暂停',possibly_stuck:'可能受阻'};
let current = null;
let busy = false;
let polling = false;
let transportError = false;
let lastStateReceivedAt = 0;
let lastFrameLoadedAt = 0;
let cameraRequestPending = false;
let cameraRequestStartedAt = 0;
let cameraError = false;
let requestError = '';
let lastRenderedTurn = -1;
let lastRenderedHistoryLength = -1;
let actionTimer = null;
let chatPinnedToBottom = true;
let chatUserInteractionUntil = 0;
let chatPointerActive = false;
let chatExpectedScrollTop = null;
let chatLastGeometry = null;
let chatFollowFrame = null;
let chatResizeObserver = null;
let telemetryStream = null;
let streamPolling = false;
let streamError = '';
let streamReceivedAt = 0;
let streamAdvancedAt = 0;
let streamIdentity = null;
let streamGeneration = 0;

function responseLabel(response){
  const type=response.action_detail?.type;
  if(type==='route_changed')return '路径请求已提交';
  if(type==='preference_changed')return '设置值已确认';
  if(type==='driving_status')return '实时状态查询';
  if(type==='eta_query')return '估计时间查询';
  if(type==='traffic_unknown')return '路况数据不足';
  if(type==='traffic_clarification')return '需要路况依据';
  if(type==='critical_event')return '告警优先';
  if(type==='weather_changed')return '天气已切换';
  if(type==='scenario')return '场景操作完成';
  if(type==='recorder')return '录制器状态更新';
  if(type==='speed_target_set')return '目标车速已设置';
  if(type==='lane_change')return '变道路径已提交';
  if(type==='drive_state')return '驾驶状态已切换';
  if(type==='direct_control')return '直接控制已执行';
  if(type==='direct_control_denied')return '分层权限拒止';
  if(type==='emergency_stop')return '紧急停车已执行';
  if(type==='destination_changed')return '目的地已更换';
  return labels[response.action_result]||response.action_result;
}
function number(value, digits=0){return typeof value==='number'&&Number.isFinite(value)?value.toFixed(digits):'—'}
function age(value){return typeof value==='number'&&Number.isFinite(value)?(value<1000?`${Math.max(0,Math.round(value))} ms`:`${(value/1000).toFixed(1)} s`):'未知'}
function el(tag, cls, content){const node=document.createElement(tag);if(cls)node.className=cls;if(content!==undefined)node.textContent=String(content);return node}
function showNotice(id, message){const node=$(id);node.hidden=!message;if(node.textContent!==message)node.textContent=message}
function eventSource(event){return event?.source==='carla_sensor'?'CARLA 传感器':event?.source==='operator'?'操作台注入':event?'来源未标注':'无事件'}
function observation(entry){return entry.response?.action_detail?.observation||entry.agent_input?.telemetry||entry.before?.telemetry||{}}
function chatGeometry(){const node=$('messages');return [node.clientWidth,node.clientHeight,node.scrollHeight].join(':')}
function followChatBottom(){
  const node=$('messages');
  if(chatPinnedToBottom){node.scrollTop=node.scrollHeight;chatExpectedScrollTop=node.scrollTop}
  chatLastGeometry=chatGeometry();
}
function scheduleChatFollow(){
  if(chatFollowFrame!==null)cancelAnimationFrame(chatFollowFrame);
  chatFollowFrame=requestAnimationFrame(()=>{chatFollowFrame=null;followChatBottom()});
}
function observeChatLayout(){
  if(!chatResizeObserver)return;
  chatResizeObserver.disconnect();chatResizeObserver.observe($('messages'));
  $('messages').querySelectorAll('.message,.empty-state').forEach(node=>chatResizeObserver.observe(node));
}

function streamHealth(){
  if(current?.mode==='memory')return {status:'memory',age:null};
  if(!telemetryStream)return {status:streamError?'disconnected':'connecting',age:null};
  if(telemetryStream.source!=='carla')return {status:'memory',age:null};
  const meta=telemetryStream.snapshot?.telemetry||{};
  const sampleAge=typeof meta.age_ms==='number'?meta.age_ms+Math.max(0,Date.now()-streamReceivedAt):null;
  if(streamError)return {status:'disconnected',age:sampleAge};
  let status=meta.status||current?.snapshot.telemetry?.status||'connecting';
  if(!telemetryStream.samples_total)status='connecting';
  if(status==='live'&&(sampleAge===null||sampleAge>2000||(streamAdvancedAt&&Date.now()-streamAdvancedAt>2500)))status='stale';
  return {status,age:sampleAge};
}
function renderStreamHealth(){
  const h=streamHealth(),data=telemetryStream,carla=current?.mode==='carla';
  const frame=data?.snapshot?.telemetry?.frame;
  const names={live:'持续采集中',stale:'等待新采样',disconnected:'数据流连接中断',connecting:'等待实际采样',memory:'内存模式不可用'};
  $('telemetryOpen').className=`telemetry-entry ${h.status}`;$('telemetryOpen').disabled=!carla;
  $('streamStatus').replaceChildren(el('i','signal-dot'),document.createTextNode(names[h.status]||'采集状态未知'));
  $('streamCount').textContent=carla&&data?.source==='carla'?`累计采样 ${number(data.samples_total)}`:'累计采样 —';
  $('streamRate').textContent=carla&&data?.source==='carla'?`${number(data.sample_rate_hz,1)} Hz`:'— Hz';
  $('streamFrame').textContent=carla?`F${number(frame)}`:'F—';
  $('streamAge').textContent=carla&&h.age!==null?`${age(h.age)} 前`:'时间未知';
  $('dialogSamples').textContent=carla&&data?.source==='carla'?number(data.samples_total):'—';
  $('dialogRate').textContent=carla&&data?.source==='carla'?`${number(data.sample_rate_hz,1)} Hz`:'—';
  $('dialogBuffer').textContent=carla&&data?.source==='carla'?`${number(data.buffer_size)} / ${number(data.buffer_capacity)}`:'— / 300';
  $('dialogFrame').textContent=carla?number(frame):'—';
  const notice=$('streamNotice');notice.className=`stream-notice ${h.status}`;
  notice.textContent=h.status==='memory'?'当前为内存逻辑演示，未持续采集 CARLA 数据。':h.status==='live'?`服务端正在持续读取 CARLA，最新输入 ${age(h.age)} 前采集。下方是已经收到的实际记录。`:h.status==='stale'?`最新采样尚未更新${h.age!==null?`，数据年龄 ${age(h.age)}`:''}；累计计数保留最后回执值，未作推算。`:h.status==='disconnected'?`数据流接口暂不可用；保留最后收到的采样与计数${h.age!==null?`（最后输入 ${age(h.age)} 前）`:''}，正在重连。${streamError?` ${streamError}`:''}`:'等待服务端返回新的 CARLA 采样；页面不会自行生成计数。';
  const canExport=carla&&data?.source==='carla'&&data.buffer_size>0;
  $('telemetryExport').setAttribute('aria-disabled',String(!canExport));
  if(canExport)$('telemetryExport').setAttribute('href','/api/telemetry-export.jsonl');else $('telemetryExport').removeAttribute('href');
  $('streamBufferNote').textContent=carla&&data?.source==='carla'?`当前缓存 ${number(data.buffer_size)} 条，容量 ${number(data.buffer_capacity)}；较早 ${number(data.dropped_samples)} 条已移出缓存。仅导出最近缓存。`:'导出内容仅为当前最近缓存，最多 300 条。';
}
function streamLight(sample){
  const light=sample.environment?.traffic_light;
  if(!light)return '—';
  if(light.affecting===false)return '不受约束';
  if(light.affecting!==true)return '—';
  return {red:'红灯',yellow:'黄灯',green:'绿灯',off:'关闭',unknown:'未知'}[String(light.state||'').toLowerCase()]||'未知';
}
function renderStreamRows(){
  const body=$('streamRows');body.replaceChildren();
  const samples=current?.mode==='carla'&&telemetryStream?.source==='carla'&&Array.isArray(telemetryStream.recent_samples)?telemetryStream.recent_samples.slice(-20).reverse():[];
  $('streamRowsLabel').textContent=`最近 ${samples.length} 条实际采样`;
  if(!samples.length){const row=el('tr'),cell=el('td',null,current?.mode==='memory'?'内存模式没有 CARLA 采样':'等待实际采样');cell.colSpan=6;row.append(cell);body.append(row);return}
  for(const sample of samples){
    const vehicle=sample.vehicle||{},front=sample.environment?.front_vehicle;
    const lane=vehicle.road_id!==null&&vehicle.road_id!==undefined?`${vehicle.road_id} / ${vehicle.lane_id??'—'}`:'—';
    const gap=front?number(front.gap_m,1):sample.environment&&Object.prototype.hasOwnProperty.call(sample.environment,'front_vehicle')?'未检出':'—';
    const row=el('tr');
    [number(sample.telemetry?.frame),number(sample.telemetry?.simulation_time_s,2),number(sample.adas?.current_speed_kmh,1),lane,streamLight(sample),gap].forEach(value=>row.append(el('td',null,value)));
    body.append(row);
  }
}
function renderStreamRaw(){
  const raw=current?.mode==='carla'&&telemetryStream?.source==='carla'?telemetryStream.snapshot:null;
  $('streamRaw').textContent=raw?JSON.stringify(raw,null,2):current?.mode==='memory'?'当前是内存逻辑演示，没有 CARLA 原始输入。':'尚未收到实际输入';
}
async function pollTelemetryStream(){
  if(streamPolling||!current||current.mode!=='carla')return;
  streamPolling=true;const generation=streamGeneration;
  try{
    const value=await api('/api/telemetry-stream');
    if(generation!==streamGeneration||current?.mode!=='carla')return;
    if(!value||value.source!=='carla')throw new Error('数据源没有返回 CARLA 采样');
    const identity=`${value.samples_total}:${value.snapshot?.telemetry?.frame??''}`;
    if(identity!==streamIdentity){streamIdentity=identity;streamAdvancedAt=Date.now()}
    telemetryStream=value;streamReceivedAt=Date.now();streamError='';
    if($('telemetryDialog').open){renderStreamRows();if($('streamRawDetails').open)renderStreamRaw()}
  }catch(error){if(generation===streamGeneration)streamError=error.name==='AbortError'?'接口响应超时。':error.message}
  finally{streamPolling=false;renderStreamHealth()}
}

async function api(path, data){
  const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),8000);
  const options={signal:controller.signal};
  if(data!==undefined)Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  try{const response=await fetch(path,options);const value=await response.json();if(!response.ok)throw new Error(value.error||`请求失败 (${response.status})`);return value}
  finally{clearTimeout(timer)}
}

function health(){
  if(!current)return {data:'connecting',camera:'waiting',dataAge:null,cameraAge:null};
  if(current.mode!=='carla')return {data:'memory',camera:'memory',dataAge:null,cameraAge:null};
  const elapsed=Math.max(0,Date.now()-lastStateReceivedAt);
  const telemetry=current.snapshot.telemetry||{};const camera=current.snapshot.camera||{};
  const dataAge=typeof telemetry.age_ms==='number'?telemetry.age_ms+elapsed:null;
  const cameraAge=typeof camera.age_ms==='number'?camera.age_ms+elapsed:null;
  let data=transportError?'disconnected':telemetry.status||'connecting';
  if(data==='live'&&(dataAge===null||dataAge>2000))data='stale';
  let image=transportError?'disconnected':camera.status||'waiting';
  if(image==='live'&&(cameraAge===null||cameraAge>2000))image='stale';
  if(cameraError)image='unavailable';
  else if(!lastFrameLoadedAt&&image==='live')image='waiting';
  return {data,camera:image,dataAge,cameraAge};
}

function renderHealth(){
  const h=health();const carla=current?.mode==='carla';
  const overall=h.data==='live'&&h.camera!=='live'?'stale':h.data;
  const stateNames={live:'数据与画面实时同步',stale:'采集存在延迟',disconnected:'连接中断',connecting:'正在连接',memory:'内存逻辑演示'};
  $('connectionBadge').className=`connection-badge ${overall}`;$('connectionLabel').textContent=stateNames[overall]||'采集不可用';
  document.querySelector('.simulation').classList.toggle('data-stale',carla&&h.data!=='live');
  const meta=current?.snapshot.telemetry||{};
  $('frameInfo').textContent=carla?`FRAME ${number(meta.frame)}`:'MEMORY DEMO';
  $('dataAge').textContent=carla?(h.dataAge===null?'采集时间未知':`${age(h.dataAge)} 前采集`):'本地内存状态';
  $('dataMeta').className=`data-meta ${h.data}`;
  $('telemetryDetail').textContent=carla?(meta.error||`${stateNames[h.data]||h.data} · ${typeof meta.simulation_time_s==='number'?`仿真 ${meta.simulation_time_s.toFixed(2)} s`:'仿真时间未知'}`):'内存演示';
  const cameraNames={live:'实时画面',stale:'画面延迟',waiting:'等待画面',disconnected:'画面连接中断',unavailable:'画面暂不可用'};
  $('cameraBadge').className=`camera-badge ${h.camera}`;$('cameraStatus').textContent=cameraNames[h.camera]||'画面状态未知';
  const cameraFrame=current?.snapshot.camera?.frame;
  if(carla&&h.camera!=='live')showNotice('cameraNotice',`${cameraNames[h.camera]||'画面状态未知'}${typeof cameraFrame==='number'?` · 最后帧 ${cameraFrame}`:''}${h.cameraAge!==null?` · ${age(h.cameraAge)} 未更新`:''}`);
  else showNotice('cameraNotice','');
  let notice='';
  if(transportError)notice='页面与服务连接中断，当前显示上次收到的数据；正在重连。';
  else if(carla&&h.data!=='live')notice=h.data==='connecting'?'正在等待 CARLA 的新采集帧。':'车辆数据未实时更新；设置操作已暂停，仍可查询连接状态。';
  else if(carla&&h.camera!=='live')notice='车辆数据仍在更新，摄像头尚未返回实时画面。';
  if(requestError)notice=requestError;
  showNotice('connectionNotice',notice);
  $('composerHint').className=`composer-hint ${h.data}`;
  $('composerHint').querySelector('span').textContent=carla?(h.data==='live'?'回答会引用当前仿真采集帧':'回答将明确说明数据可用性'):'当前为本地内存逻辑演示';
  document.querySelectorAll('[data-live-control]').forEach(button=>button.disabled=busy||(carla&&h.data!=='live')||!current);
  $('cameraModeBtn').disabled=busy||!carla||h.data!=='live';
  $('resetBtn').disabled=busy;$('clearEvent').disabled=busy||!current;$('eventScenario').disabled=busy||!current;
  if(current&&!current.snapshot.latest_event&&!(current.snapshot.events||[]).length)renderEvent(current.snapshot);
  renderStreamHealth();
}

function renderState(view){
  if(!view?.snapshot)throw new Error('服务没有返回可用状态');
  current=view;lastStateReceivedAt=Date.now();transportError=false;
  const state=view.snapshot,adas=state.adas||{},nav=state.navigation||{},env=state.environment||{},vehicle=state.vehicle||{};
  const carla=view.mode==='carla';$('carlaScene').hidden=!carla;$('memoryScene').hidden=carla;
  if(!carla){telemetryStream=null;streamError='';streamIdentity=null}
  $('modeTag').textContent=carla?'CARLA SIM':'MEMORY DEMO';
  $('cameraModeBtn').hidden=!carla;$('mapName').hidden=!carla;
  if(carla){$('mapName').textContent=state.map?.split('/').at(-1)||'CARLA';$('cameraLabel').textContent=state.camera_mode==='chase'?'车外跟随视角':'前视摄像头';$('cameraModeBtn').textContent=state.camera_mode==='chase'?'切换前视视角':'切换车外视角'}
  $('noaLabel').textContent=carla?'TM 自动驾驶':'NOA 状态';
  $('distanceUnit').textContent=carla?`级 / ${number(adas.follow_distance_m)} m`:'级';
  $('disclaimer').textContent=carla?'数据来自 CARLA 仿真 API / 传感器；前车间距由仿真位置计算，路线 ETA 为几何估计。':'内存逻辑演示 · 不连接 CARLA 或真实车辆';
  const routeList=Array.isArray(nav.routes)?nav.routes:[];
  $('noaStatus').textContent=adas.noa_status==='active'?'ACTIVE':adas.noa_status==='off'?'OFF':'—';
  $('noaStatus').className=adas.noa_status==='active'?'active':adas.noa_status==='off'?'off':'';
  $('speedTargetLabel').textContent=carla?(typeof adas.speed_target_kmh==='number'?`目标车速 ${number(adas.speed_target_kmh)} km/h${adas.noa_status==='off'?' · TM 已暂停':''}`:(adas.noa_status==='off'?'TM 已暂停 · 直接控制开放':'目标车速 按限速')):'目标车速 按限速';
  $('speed').textContent=number(adas.current_speed_kmh,1);$('speedLimit').textContent=number(adas.speed_limit_kmh);$('distance').textContent=number(adas.follow_distance_level);
  $('turnCount').textContent=`${view.turn??0} 轮`;$('toggleNoa').textContent=adas.noa_status==='active'?'操作台：暂停 TM':'操作台：恢复 TM';
  $('frontGap').textContent=env.front_vehicle?`${number(env.front_vehicle.gap_m,1)} m`:carla&&env.motion?'范围内未检出':'—';
  $('frontSpeed').textContent=env.front_vehicle?`${number(env.front_vehicle.speed_kmh,1)} km/h`:'—';
  $('nearbyCount').textContent=typeof env.nearby_vehicle_count==='number'?`${env.nearby_vehicle_count} 辆`:'—';
  $('motionState').textContent=motionLabels[env.motion?.state]||(carla?'等待车况':'内存状态');
  $('motionState').className=`motion-tag ${env.motion?.state||'unknown'}`;
  $('laneInfo').textContent=vehicle.road_id!==undefined&&vehicle.road_id!==null?`道路 ${vehicle.road_id} / 车道 ${vehicle.lane_id??'—'}${vehicle.is_junction?' · 路口':''}`:'道路 / 车道 —';
  const loc=vehicle.location;$('positionInfo').textContent=loc?`${number(loc.x,1)}, ${number(loc.y,1)}, ${number(loc.z,1)}`:'—';
  const controls=vehicle.control;$('controlInfo').textContent=controls?`${number(controls.throttle,2)} / ${number(controls.brake,2)}`:'—';
  const sensors=state.sensors||{};
  const gnss=sensors.gnss;$('gnssInfo').textContent=carla&&gnss?.status==='live'?`${number(gnss.latitude,5)}, ${number(gnss.longitude,5)}`:carla?'未就绪':'—';
  const lidar=sensors.lidar;$('obstacleInfo').textContent=carla&&lidar?.status==='live'?(typeof lidar.nearest_obstacle_m==='number'?`${number(lidar.nearest_obstacle_m,1)} m（${number(lidar.points)} 点）`:'回波中无障碍'):'—';
  const radar=sensors.radar;$('radarInfo').textContent=carla&&radar?.status==='live'?(typeof radar.nearest_m==='number'?`最近 ${number(radar.nearest_m,1)} m / ${number(radar.target_count)} 个`:'无目标'):'—';
  const pedestrians=env.pedestrians;$('pedestrianInfo').textContent=carla&&pedestrians?(typeof pedestrians.nearby_count_30m==='number'?`${number(pedestrians.nearby_count_30m)} 人${typeof pedestrians.nearest_m==='number'?`（最近 ${number(pedestrians.nearest_m,1)} m）`:''}`:'—'):'—';
  const weather=env.weather;$('weatherInfo').textContent=weather?(carla?`${weather.label||weather.preset||'—'}${typeof weather.precipitation_pct==='number'?` · 雨 ${number(weather.precipitation_pct)}%`:''}${typeof weather.fog_density_pct==='number'?` · 雾 ${number(weather.fog_density_pct)}%`:''}`:(weather.label||'—')):'—';
  const recorder=state.recorder,scenario=state.scenario;
  $('recorderInfo').textContent=recorder?(carla?`${recorder.active?'录制中':'未录制'} · 场景：${scenario?.active_kind||'无'}`:'—'):'—';
  $('syncInfo').textContent=carla?(state.simulation_mode==='sync'?'同步 20 Hz':'异步'):'—';
  $('syncModeBtn').textContent=state.simulation_mode==='sync'?'切回异步模式':'切换同步模式';
  const light=env.traffic_light||{};let lightState='unknown',lightText='信号灯未知';
  if(light.affecting===false){lightState='none';lightText='未受信号灯约束'}
  else if(light.affecting===true){const key=String(light.state||'').toLowerCase();lightState=['red','yellow','green'].includes(key)?key:'unknown';lightText={red:'红灯',yellow:'黄灯',green:'绿灯'}[lightState]||'灯色未知'}
  $('trafficLight').className=`light-label ${lightState}`;$('trafficLight').querySelector('span').textContent=lightText;
  $('routeCount').textContent=`${routeList.length} 条 · 几何估计`;const routes=$('routes');routes.replaceChildren();
  for(const route of routeList){const selected=route.id===nav.current_route_id;const row=el('div','route-item'+(selected?' selected':''));const left=el('div');left.append(el('strong',null,route.id==='route_a'?'原始路径 A':'备选路径 B'),el('small',null,selected?'已提交记录':'候选路径'));const eta=el('span','eta',number(route.eta_min,1));eta.append(el('small',null,'min'));row.append(left,eta);routes.append(row)}
  renderEvent(state);
  const history=Array.isArray(view.history)?view.history:[];
  if(view.turn!==lastRenderedTurn||history.length!==lastRenderedHistoryLength){renderHistory(history);renderTrace(history.at(-1));lastRenderedTurn=view.turn;lastRenderedHistoryLength=history.length}
  renderHealth();
}

function renderEvent(state){
  const alert=$('alertContent');alert.replaceChildren();const event=state.latest_event||(state.events||[]).at(-1);
  const dataStatus=health().data,live=dataStatus==='memory'||dataStatus==='live';$('eventSource').textContent=event?eventSource(event):live?'无事件':'状态未更新';
  alert.className=event?(event.severity==='critical'?'alert-active':'alert-warning'):'alert-idle';
  const title=event?(event.code==='takeover_required'?'模拟接管告警':event.code==='collision'?'检测到碰撞':event.code==='lane_invasion'?'检测到压线':event.code||'车辆事件'):(live?'当前无事件':'事件状态未更新');
  const subtitle=event?`${event.message||''}${typeof event.frame==='number'?` · F${event.frame}`:''}`:(live?'CARLA 传感器与操作台事件将在此显示':'等待新的采集帧');
  const content=el('div');content.append(el('strong',null,title),el('small',null,subtitle));alert.append(el('span',null,event?'!':'—'),content);
}

function renderHistory(history){
  const messages=$('messages'),previousScroll=messages.scrollTop;messages.replaceChildren();
  if(!history.length){const empty=el('div','empty-state');empty.append(el('span','empty-icon','✦'),el('strong',null,'从一个驾驶问题开始'),el('p',null,'你的原话、DA 的判断和数据依据都会留在这里。'));messages.append(empty);observeChatLayout();return}
  for(const item of history){
    const user=el('div','message user',item.utterance);user.append(el('small',null,`用户 · ${item.time||''}`));
    const reply=el('div','message assistant');const obs=observation(item);
    const head=el('div','assistant-head');head.append(el('span','assistant-avatar','DA'),el('span',null,'Driving Assistant'));
    if(typeof obs.frame==='number')head.append(el('span','evidence-frame',`依据 F${obs.frame}`));reply.append(head,el('div','message-text',item.response.reply_text));
    const decision=item.response.action_detail?.decision;
    if(decision){const detail=el('div','message-decision');if(decision.reason)detail.append(el('strong',null,`判断：${decision.reason}`));if(item.observed_change?.length)detail.append(el('span',null,`状态：${item.observed_change.join('；')}`));const more=el('details','message-more');more.append(el('summary',null,'数据依据与执行回执'));if(decision.evidence?.length)more.append(el('span',null,`依据：${decision.evidence.join(' · ')}`));more.append(el('span',null,`网关：${decision.gateway||'无写操作'}`));detail.append(more);reply.append(detail)}
    reply.append(el('small','response-status',responseLabel(item.response)));messages.append(user,reply);
  }
  if(chatPinnedToBottom){followChatBottom();scheduleChatFollow()}else messages.scrollTop=previousScroll;
  observeChatLayout();
}

function renderTrace(entry){
  const trace=$('trace'),badge=$('resultBadge');trace.replaceChildren();
  if(!entry){trace.className='trace-empty';trace.textContent='本轮使用了什么状态，发出了什么请求，都在这里。';badge.className='result-badge neutral';badge.textContent='等待指令';return}
  trace.className='';const r=entry.response,result=r.action_result;badge.className='result-badge '+(result==='success'?'success':result==='rejected'?'danger':'warning');badge.textContent=responseLabel(r);
  const decision=r.action_detail?.decision||{},calls=entry.gateway_calls||[],obs=observation(entry);
  const gate=decision.gateway||(calls.length?calls.map(c=>c[0]).join('、'):'无写操作');
  const safety=result==='success'?'检查通过':result==='rejected'?'安全拒止':result==='unavailable'?'数据或回执不可用':result==='clarification'?'需要澄清':'保持状态';
  const grid=el('div','trace-grid');
  [['INTENT',intentLabels[entry.intent]||entry.intent],['OBSERVATION',typeof obs.frame==='number'?`仿真帧 ${obs.frame}`:'无仿真帧'],['CHECK',safety],['GATEWAY',gate]].forEach(([label,value])=>{const step=el('div','trace-step');step.append(el('span',null,label),el('strong',null,value));grid.append(step)});
  const note=el('div','trace-note');note.append(el('strong',null,decision.reason||safety));if(decision.evidence?.length)note.append(el('span',null,decision.evidence.join(' · ')));trace.append(grid,note);
}

async function action(path,data){
  if(busy)return;busy=true;requestError='';if(path==='/api/command'||path==='/api/event')chatPinnedToBottom=true;$('commandForm').querySelector('button').disabled=true;renderHealth();
  try{const view=await api(path,data);if(path==='/api/reset'){streamGeneration++;telemetryStream=null;streamIdentity=null;streamAdvancedAt=0;streamError=''}renderState(view);const toast=$('liveAction');if(view.mode==='carla'&&view.entry){toast.textContent=`${responseLabel(view.entry.response)} · 查看右侧记录`;toast.className='live-action'+(view.entry.response.action_result==='success'?'':' warning');toast.hidden=false;clearTimeout(actionTimer);actionTimer=setTimeout(()=>toast.hidden=true,3500)}else if(path==='/api/reset'){toast.hidden=true;lastFrameLoadedAt=0;cameraError=false;pollTelemetryStream()}}
  catch(error){requestError=`本次请求未完成：${error.name==='AbortError'?'服务响应超时；请先检查状态后再决定是否重试':error.message}`;showNotice('connectionNotice',requestError)}
  finally{busy=false;$('commandForm').querySelector('button').disabled=false;renderHealth()}
}
async function pollState(){
  if(polling||busy)return;polling=true;
  try{renderState(await api('/api/state'));if(!telemetryStream)pollTelemetryStream()}catch(error){transportError=true;renderHealth()}finally{polling=false}
}
document.querySelectorAll('[data-utterance]').forEach(button=>button.addEventListener('click',()=>action('/api/command',{utterance:button.dataset.utterance})));
$('eventScenario').addEventListener('click',()=>action('/api/event',{}));
$('commandForm').addEventListener('submit',event=>{event.preventDefault();const input=$('utterance'),utterance=input.value.trim();if(!utterance)return;input.value='';action('/api/command',{utterance})});
$('resetBtn').addEventListener('click',()=>action('/api/reset',{}));
$('clearEvent').addEventListener('click',()=>action('/api/clear-event',{}));
$('toggleNoa').addEventListener('click',()=>action('/api/noa',{status:current?.snapshot.adas?.noa_status==='active'?'off':'active'}));
$('cameraModeBtn').addEventListener('click',()=>{lastFrameLoadedAt=0;cameraError=false;action('/api/camera-mode',{mode:current?.snapshot.camera_mode==='chase'?'front':'chase'})});
$('syncModeBtn').addEventListener('click',()=>action('/api/sync-mode',{enabled:current?.snapshot.simulation_mode!=='sync'}));
$('telemetryOpen').addEventListener('click',()=>{if(current?.mode!=='carla')return;$('telemetryDialog').showModal();renderStreamHealth();renderStreamRows();renderStreamRaw();pollTelemetryStream()});
$('telemetryClose').addEventListener('click',()=>$('telemetryDialog').close());
$('telemetryDialog').addEventListener('close',()=>$('telemetryOpen').focus({preventScroll:true}));
$('telemetryDialog').addEventListener('click',event=>{if(event.target!==$('telemetryDialog'))return;const rect=$('telemetryDialog').getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)$('telemetryDialog').close()});
$('streamRawDetails').addEventListener('toggle',()=>{if($('streamRawDetails').open)renderStreamRaw()});
const camera=$('cameraFrame');
const chatMessages=$('messages');
chatMessages.tabIndex=0;
chatMessages.addEventListener('scroll',()=>{
  const geometry=chatGeometry(),userInput=chatPointerActive||performance.now()<chatUserInteractionUntil;
  const expected=chatExpectedScrollTop!==null&&Math.abs(chatMessages.scrollTop-chatExpectedScrollTop)<2;
  const geometryChanged=chatLastGeometry!==null&&geometry!==chatLastGeometry;
  if(userInput||(!expected&&!geometryChanged&&chatFollowFrame===null))chatPinnedToBottom=chatMessages.scrollHeight-chatMessages.clientHeight-chatMessages.scrollTop<28;
  chatExpectedScrollTop=null;chatLastGeometry=geometry;
},{passive:true});
chatMessages.addEventListener('wheel',event=>{chatUserInteractionUntil=performance.now()+500;if(event.deltaY<0)chatPinnedToBottom=false},{passive:true});
chatMessages.addEventListener('touchstart',()=>{chatUserInteractionUntil=performance.now()+800},{passive:true});
chatMessages.addEventListener('touchmove',()=>{chatUserInteractionUntil=performance.now()+800},{passive:true});
chatMessages.addEventListener('pointerdown',()=>{chatPointerActive=true;chatUserInteractionUntil=performance.now()+800},{passive:true});
window.addEventListener('pointerup',()=>{if(chatPointerActive)chatUserInteractionUntil=performance.now()+250;chatPointerActive=false},{passive:true});
chatMessages.addEventListener('keydown',event=>{
  if(['PageUp','Home','ArrowUp'].includes(event.key)||(event.key===' '&&event.shiftKey)){chatPinnedToBottom=false;chatUserInteractionUntil=performance.now()+500}
  else if(event.key==='End'){chatPinnedToBottom=true;scheduleChatFollow()}
  else if(['PageDown','ArrowDown',' '].includes(event.key))chatUserInteractionUntil=performance.now()+500;
});
if(typeof ResizeObserver!=='undefined'){chatResizeObserver=new ResizeObserver(scheduleChatFollow);observeChatLayout()}
camera.addEventListener('load',()=>{cameraRequestPending=false;cameraError=false;lastFrameLoadedAt=Date.now();$('cameraWaiting').hidden=true;renderHealth()});
camera.addEventListener('error',()=>{cameraRequestPending=false;cameraError=true;if(!lastFrameLoadedAt)$('cameraWaiting').hidden=false;renderHealth()});
pollState();
setInterval(pollState,1000);
setInterval(pollTelemetryStream,1000);
setInterval(()=>{if(cameraRequestPending&&Date.now()-cameraRequestStartedAt>5000)cameraRequestPending=false;if(current?.mode==='carla'&&!cameraRequestPending){cameraRequestPending=true;cameraRequestStartedAt=Date.now();camera.src=`/api/frame?t=${Date.now()}`}},400);
setInterval(renderHealth,500);

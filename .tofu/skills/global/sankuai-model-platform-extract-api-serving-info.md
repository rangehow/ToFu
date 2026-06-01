---
name: sankuai-model-platform-extract-api-serving-info
description: 从美团模型平台(model.sankuai.com)提取API Serving节点IP/端口：通过Vuex store获取taskId，调用data-hub API获取节点结果；含完整API路径和参数格式
enabled: true
tags: [browser, canvas, vue, vuex, sankuai, model-platform, api, xpath]
created: 2026-03-17T11:53:31Z
updated: 2026-03-18T18:05:30Z
---

# 美团模型平台 - 提取API Serving节点信息

## 场景
从 `model.sankuai.com/user/modelManage/experimentalTaskDetail/{experimentId}` 页面提取所有 API Serving 节点的 IP 和端口。

## 关键技术要点

### 1. 页面结构
- 页面使用 **Canvas** 渲染 DAG 图（在 `.graph-container.relative` 内）
- 节点信息**不在DOM中**，必须通过 **Vuex store** 或 **API** 获取
- 不要尝试 DOM 选择器或截图 OCR

### 2. Vuex Store 路径
```javascript
const store = document.querySelector('#app').__vue__.$store;
const mm = store.state.modelManage;

// 获取实验配置详情（含所有节点定义）
const config = mm.experimentConfigDetail;

// 获取真正的 taskId（注意不是URL里的experimentId！）
const taskId = config.taskId.taskId;  // e.g. 996035

// 获取所有节点
const allNodes = config.configDetail.nodes;

// 过滤 API Serving 节点
const apiNodes = allNodes.filter(n => n.nodeName.includes('API Serving'));
// 每个节点有 nodeId, nodeType(14=API Serving), nodeName, nodeParam
```

### 3. API 调用（两套路径，不可混用！）

#### 获取任务状态 → 得到 taskDetailId 和 nodeId 映射
```
POST /webApi/model/experiment/task/status
Body: {"taskId": 996035, "retryNum": 0, "version": 1}
Response: {data: {taskStatus, nodeStatus: [{taskDetailId, nodeId, nodeStatus}, ...]}}
```

#### 获取节点结果（IP/PORT）⚠️ 用 data-hub 路径！
```
POST /webApi/data-hub/experiment/task/node/result
Body: {"taskId": 996035, "nodeId": 1666363}
Response: {data: {outputs: [{key: "模型服务IP", value: "x.x.x.x"}, {key: "模型服务PORT", value: "8080"}]}}
```

### 4. 完整提取脚本（一步到位）
```javascript
(()=>{
  const store = document.querySelector('#app').__vue__.$store;
  const mm = store.state.modelManage;
  const config = mm.experimentConfigDetail;
  const taskId = config.taskId.taskId;
  
  // Find API Serving nodes
  const apiNodes = config.configDetail.nodes
    .filter(n => n.nodeName.includes('API Serving'));
  
  // Fetch results for each
  const results = [];
  for(const node of apiNodes){
    const x = new XMLHttpRequest();
    x.open('POST', '/webApi/data-hub/experiment/task/node/result', false);
    x.setRequestHeader('Content-Type', 'application/json');
    x.send(JSON.stringify({taskId, nodeId: node.nodeId}));
    const r = JSON.parse(x.responseText);
    const ip = r.data.outputs.find(o => o.key === '模型服务IP')?.value;
    const port = r.data.outputs.find(o => o.key === '模型服务PORT')?.value;
    results.push({name: node.nodeName, nodeId: node.nodeId, ip, port});
  }
  return JSON.stringify(results);
})()
```

### 5. 常见陷阱
- **URL里的ID (276651) 是 experimentId**，不是 taskId！真正的 taskId 在 Vuex store 的 `experimentConfigDetail.taskId.taskId` 里
- **两套 API 路径不可混用**：`/webApi/model/experiment/task/status` vs `/webApi/data-hub/experiment/task/node/result`
- **taskDetailId vs nodeId**: task/status 返回的 taskDetailId 在 data-hub/node/result 中不好用（会报 TooManyResults），应使用 `{taskId, nodeId}` 组合
- **Tab ID 不稳定**：美团平台页面可能导致 extension tab 频繁失效，需要反复 list_tabs 获取最新 ID


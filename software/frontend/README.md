# RP1 测试大屏

React 19 / Vite / TypeScript / ECharts前端，与当前 `reliability_v1` API配套。

页面包括总控、时长、MTBF、样品/计划、执行详情、在线测试、工位监控、审批、中央归档、历史CSV导入、详细曲线和账户管理。首页保留部位模型和卡片轮播；曲线支持缩放、平移、参考、多执行叠加/分面。

```bash
cd software/frontend
npm ci
npm run check
```

通过 [软件部署](../README.md) 启动，访问配置的HTTPS地址（默认8443）。数据来自 `/api/v1`，无记录时显示空状态。

入口 `src/main.tsx` → `App.tsx`，路由 `src/navigation.ts`，页面 `src/pages/`；API封装、图表和静态资源随源码提供。

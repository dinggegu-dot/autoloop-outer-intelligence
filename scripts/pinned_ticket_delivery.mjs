import {execFileSync} from 'node:child_process';
import {mkdtempSync, readFileSync, realpathSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

export function pinnedIO(raw, target, approved, report) {
  const {number, repo, head, mode} = target;
  const prPath = 'pulls/' + number;
  const labelPath = 'issues/' + number + '/labels/';
  const withdraw = new Set(['deliver','deliver-review']);
  const pending = new Set(mode === 'review' ? withdraw : ['deliver']);

  async function current() {
    approved();
    const pr = await raw.api(prPath);
    if (pr.number !== number || pr.head?.sha !== head || pr.base?.ref !== 'main'
        || (target.branch && pr.head.ref !== target.branch)
        || pr.head.repo?.full_name?.toLowerCase() !== repo.toLowerCase()
        || pr.auto_merge || pr.labels.some(label => withdraw.has(label.name))) {
      throw new Error('当前 PR 与本单授权不符，或正由其他交付轮接管');
    }
    return {...pr, labels:[...pr.labels, ...Array.from(pending, name => ({name}))]};
  }

  async function independentlyReviewed(check, params) {
    await current();
    const result = await check(...params);
    await current();
    if (result && ('exempt' in result || 'docsOnly' in result)) {
      throw new Error('仓库按策略免审，本工单缺少独立审查证据');
    }
    return result;
  }
  return {...raw, report, enableAuto:undefined,
    review(...params) { return independentlyReviewed(raw.review,params); },
    ...(raw.verifyReview ? {verifyReview(...params) { return independentlyReviewed(raw.verifyReview,params); }} : {}),
    async api(path, data, method) {
      if (path === prPath && !method) return current();
      if (path === prPath + '/update-branch') {
        throw new Error('分支须先与 main 对齐，再由本人授权新的精确提交');
      }
      if (path.startsWith(labelPath) && method === 'DELETE' && withdraw.has(path.slice(labelPath.length))) {
        await current();
        pending.delete(path.slice(labelPath.length));
        return {};
      }
      if (method || data) {
        await current();
        if ((path !== 'statuses/' + head && path !== prPath + '/ready')
            || (path === prPath + '/ready' && mode !== 'release')) {
          throw new Error('不允许对未授权版本写入交付状态');
        }
      }
      return raw.api(path, data, method);
    },
    async merge(pr, config) {
      if (mode !== 'release' || pr.head.sha !== head) throw new Error('本单未授权合并当前提交');
      await current();
      return raw.merge(pr, config);
    },
  };
}

async function main() {
  const [installed, hook, targetJson] = process.argv.slice(2);
  const local = realpathSync(installed);
  if (hook === '--validate') {
    const {settings} = await import(pathToFileURL(join(local,'plugins/delivery/project.ts')));
    settings(JSON.parse(readFileSync(0,'utf8')));
    return;
  }
  const target = JSON.parse(targetJson);
  const {deliver} = await import(pathToFileURL(join(local,'plugins/delivery/delivery.ts')));
  const {createIO} = await import(pathToFileURL(join(local,'plugins/delivery/github.ts')));
  const {runHook} = await import(pathToFileURL(join(local,'plugins/delivery/handoff.ts')));
  const work = mkdtempSync(join(tmpdir(),'autoloop-ticket-delivery-'));
  const approved = () => {
    const status = JSON.parse(execFileSync(hook,['status',target.ticket],{encoding:'utf8',timeout:3000}));
    const auth = status.authorization;
    if (auth?.repo.toLowerCase() !== target.repo.toLowerCase() || auth?.number !== target.number
        || auth?.head !== target.head || auth?.mode !== target.mode || auth?.id !== target.authorization || !auth?.artifact_match) throw new Error('本单的发布授权已更新');
  };
  let completedMerge, activeShip;
  const report = (phase, head, details={}) => {
    approved();
    if (head !== target.head) throw new Error('交付工具回执与工单提交不符');
    completedMerge = details.merge ?? completedMerge;
    activeShip = details.ship ?? activeShip;
    runHook(hook,{repo:target.repo,number:target.number,head,phase,authorization:target.authorization,at:new Date().toISOString(),
                  merge:completedMerge,ship:activeShip,...details},3000);
  };
  try {
    const raw = createIO(target.repo, work, {report});
    const result = await deliver(target.repo,target.number,pinnedIO(raw,target,approved,report));
    if (result.findings?.length || (!result.merged && !result.reviewOnly)) {
      report('returned',target.head,{detail:result.findings?.length?'review':'delivery'});
      process.exitCode = 1;
    }
    console.log(result.summary);
  } catch (error) {
    try {
      const detail=error.message === '仓库按策略免审，本工单缺少独立审查证据'
        ? 'missing_independent_review' : 'delivery';
      report('returned',target.head,{detail});
    } catch {
      console.error('不再持有当前授权或交付回执未能登记');
    }
    process.exitCode = 1;
  } finally {
    rmSync(work,{recursive:true,force:true});
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}

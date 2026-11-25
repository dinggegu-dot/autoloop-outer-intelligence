import assert from 'node:assert/strict';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {test} from 'node:test';
import {pinnedIO} from './pinned_ticket_delivery.mjs';

const installed = process.env.AUTOLOOP_DELIVERY_TEST_APP;
const head = 'a'.repeat(40), base = 'b'.repeat(40), merge = 'c'.repeat(40);

function fixture(mode, behind=false, exempt=false) {
  const used = [], reports = [];
  const repo = 'fixture/app';
  const pr = {number:1,state:'open',draft:false,labels:[],head:{sha:head,repo:{full_name:repo}},
              base:{ref:'main'},user:{login:'owner'}};
  const config={repo,internal:['docs/**/*.md'],check:{workflow:'check.yml',job:'check'},ship:{workflow:'ship.yml'},
                review:{points:'AGENTS.md'},health:{url:'https://fixture.invalid/health',field:'commit',
                pattern:'(?P<sha>[0-9a-f]{40})',require:{ok:true}}};
  const raw = {
    async api(path, body, method) {
      assert.notEqual(method,'DELETE');
      assert.ok(!path.includes('update-branch'));
      if (path.startsWith('statuses/')) {
        used.push([path,body.state]);
        return {};
      }
      if (path === 'pulls/1') return structuredClone(pr);
      if (path === 'git/ref/heads/main') return {object:{sha:base}};
      if (path.startsWith('contents/ops/delivery.json')) return {content:Buffer.from(JSON.stringify(config)).toString('base64')};
      if (path.startsWith('actions/workflows/check.yml')) return {workflow_runs:[{id:11,head_sha:head,event:'pull_request',status:'completed',conclusion:'success'}]};
      if (path.startsWith('actions/runs/11/jobs')) return {jobs:[{name:'check',status:'completed',conclusion:'success'}]};
      if (path.startsWith('actions/workflows/ship.yml')) return {workflow_runs:[{id:23,head_sha:merge,event:'push',status:'completed',conclusion:'success'}]};
      throw new Error('unexpected fixture read '+path);
    },
    async contains() { return !behind; },
    async releaseRequired() { return true; },
    async live() { return {sha:merge,release:merge,ok:true}; },
    async review() { used.push('review'); return exempt ? {exempt:'fixture'} : []; },
    async verifyReview() { return []; },
    async merge(candidate) {
      assert.equal(candidate.head.sha,head);
      used.push('merge');
      Object.assign(pr,{merged_at:'now',merge_commit_sha:merge,state:'closed'});
    },
    async wait() { throw new Error('unexpected fixture wait'); },
  };
  return {io:pinnedIO(raw,{repo,head,number:1,mode},()=>{},(...entry)=>reports.push(entry)), pr, used, reports, repo};
}

test('existing plugin checks and verifies the fixed commit using simulated GitHub', {skip:!installed}, async () => {
  const {deliver} = await import(pathToFileURL(join(installed,'plugins/delivery/delivery.ts')));
  const release=fixture('release');
  assert.equal((await deliver(release.repo,1,release.io)).merged,merge);
  assert.ok(release.used.includes('review'));
  assert.ok(release.used.includes('merge'));
  assert.deepEqual(release.reports.at(-1),['live',head,{merge,ship:23}]);
  const review=fixture('review');
  assert.equal((await deliver(review.repo,1,review.io)).reviewOnly,true);
  assert.equal(review.used.includes('merge'),false);
  const behind=fixture('release',true);
  await assert.rejects(deliver(behind.repo,1,behind.io),/重新授权|新的精确提交/);
  assert.equal(behind.used.includes('merge'),false);
  const exemption=fixture('release',false,true);
  await assert.rejects(deliver(exemption.repo,1,exemption.io),/缺少独立审查证据/);
  assert.equal(exemption.used.includes('merge'),false);
  assert.equal(exemption.reports.some(row=>['reviewed','merging','live'].includes(row[0])),false);
});

test('moved or already delegated PR cannot be merged', async () => {
  const job=fixture('release');
  job.pr.head.sha='d'.repeat(40);
  await assert.rejects(job.io.api('pulls/1'),/授权不符/);
  assert.equal(job.used.includes('merge'),false);
});

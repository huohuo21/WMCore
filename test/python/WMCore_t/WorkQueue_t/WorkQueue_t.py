#!/usr/bin/env python
"""
_WorkQueue_t_

WorkQueue tests
"""

import unittest
import os
import pickle
import threading
import time

from WMCore.Configuration import Configuration
from WMCore.WorkQueue.WorkQueue import WorkQueue, globalQueue, localQueue
from WMCore.WorkQueue.WorkQueueExceptions import *
from WMCore.Services.WorkQueue.WorkQueue import WorkQueue as WorkQueueService

from WMCore.WMSpec.StdSpecs.ReReco import rerecoWorkload as rerecoWMSpec, \
                                          getTestArguments as getRerecoArgs
from WMQuality.Emulators.WMSpecGenerator.Samples.TestMonteCarloWorkload \
    import monteCarloWorkload, getMCArgs

from WMQuality.Emulators.DataBlockGenerator import Globals
from WMQuality.Emulators.DataBlockGenerator.Globals import GlobalParams
from WMQuality.Emulators.DataBlockGenerator.DataBlockGenerator \
     import DataBlockGenerator
from WMCore.DAOFactory import DAOFactory
from WMQuality.Emulators import EmulatorSetup

from WMCore_t.WorkQueue_t.WorkQueueTestCase import WorkQueueTestCase
from WMCore_t.WMSpec_t.samples.MultiTaskProductionWorkload \
                                import workload as MultiTaskProductionWorkload
from WMCore.Services.EmulatorSwitch import EmulatorHelper

from WMCore.ACDC.DataCollectionService import DataCollectionService
from WMCore.DataStructs.Run import Run
from WMCore.Services.UUID import makeUUID
from WMCore.WMBS.Job import Job
from WMCore.DataStructs.File import File as WMFile
from WMCore.WMSpec.WMWorkload import WMWorkload, WMWorkloadHelper
from WMCore.ResourceControl.ResourceControl import ResourceControl
from WMCore.Lexicon import sanitizeURL


rerecoArgs = getRerecoArgs()
mcArgs = getMCArgs()
parentProcArgs = getRerecoArgs()
parentProcArgs.update(IncludeParents = "True")

def rerecoWorkload(workloadName, arguments):
    wmspec = rerecoWMSpec(workloadName, arguments)
    #wmspec.setStartPolicy("DatasetBlock")
    return wmspec

def getFirstTask(wmspec):
    """Return the 1st top level task"""
    # http://www.logilab.org/ticket/8774
    # pylint: disable-msg=E1101,E1103
    return wmspec.taskIterator().next()

def syncQueues(queue):
    """Sync parent & local queues and split work
        Workaround having to wait for couchdb replication and splitting polling
    """
    queue.backend.forceQueueSync()
    work = queue.processInboundWork()
    queue.performQueueCleanupActions()
    queue.backend.forceQueueSync()
    return work

class WorkQueueTest(WorkQueueTestCase):
    """
    _WorkQueueTest_
    
    """
    def setUp(self):
        """
        If we dont have a wmspec file create one
        """
        EmulatorHelper.setEmulators(phedex = True, dbs = True, 
                                    siteDB = True, requestMgr = False)
        #set up WMAgent config file for couchdb
        self.configFile = EmulatorSetup.setupWMAgentConfig()

        WorkQueueTestCase.setUp(self)

        # Basic production Spec
        self.spec = monteCarloWorkload('testProduction', mcArgs)
        getFirstTask(self.spec).setSiteWhitelist(['T2_XX_SiteA', 'T2_XX_SiteB'])
        getFirstTask(self.spec).addProduction(totalevents = 10000)
        self.spec.setSpecUrl(os.path.join(self.workDir, 'testworkflow.spec'))
        self.spec.save(self.spec.specUrl())

        # Sample Tier1 ReReco spec
        self.processingSpec = rerecoWorkload('testProcessing', rerecoArgs)
        self.processingSpec.setSpecUrl(os.path.join(self.workDir,
                                                    'testProcessing.spec'))
        self.processingSpec.save(self.processingSpec.specUrl())

        # Sample Tier1 ReReco spec
        self.parentProcSpec = rerecoWorkload('testParentProcessing', parentProcArgs)
        self.parentProcSpec.setSpecUrl(os.path.join(self.workDir,
                                                    'testParentProcessing.spec'))
        self.parentProcSpec.save(self.parentProcSpec.specUrl())

        # ReReco spec with blacklist
        self.blacklistSpec = rerecoWorkload('blacklistSpec', rerecoArgs)
        self.blacklistSpec.setSpecUrl(os.path.join(self.workDir,
                                                    'testBlacklist.spec'))
        getFirstTask(self.blacklistSpec).data.constraints.sites.blacklist = ['T2_XX_SiteA']
        self.blacklistSpec.save(self.blacklistSpec.specUrl())

        # ReReco spec with whitelist
        self.whitelistSpec = rerecoWorkload('whitelistlistSpec', rerecoArgs)
        self.whitelistSpec.setSpecUrl(os.path.join(self.workDir,
                                                    'testWhitelist.spec'))
        getFirstTask(self.whitelistSpec).data.constraints.sites.whitelist = ['T2_XX_SiteB']
        self.whitelistSpec.save(self.whitelistSpec.specUrl())
        # setup Mock DBS and PhEDEx
        inputDataset = getFirstTask(self.processingSpec).inputDataset()
        self.dataset = "/%s/%s/%s" % (inputDataset.primary,
                                     inputDataset.processed,
                                     inputDataset.tier)

        # Create queues
        globalCouchUrl = "%s/%s" % (self.testInit.couchUrl, self.globalQDB)
        self.globalQueue = globalQueue(DbName = self.globalQDB,
                                       InboxDbName = self.globalQInboxDB,
                                       QueueURL = globalCouchUrl)
#        self.midQueue = WorkQueue(SplitByBlock = False, # mid-level queue
#                            PopulateFilesets = False,
#                            ParentQueue = self.globalQueue,
#                            CacheDir = None)
        # ignore mid queue as it causes database duplication's
        # copy jobStateMachine couchDB configuration here since we don't want/need to pass whole configuration
        jobCouchConfig = Configuration()
        jobCouchConfig.section_("JobStateMachine")
        jobCouchConfig.JobStateMachine.couchurl = os.environ["COUCHURL"]
        jobCouchConfig.JobStateMachine.couchDBName = "testcouchdb"
        # copy bossAir configuration here since we don't want/need to pass whole configuration
        bossAirConfig = Configuration()
        bossAirConfig.section_("BossAir")
        bossAirConfig.BossAir.pluginDir = "WMCore.BossAir.Plugins"
        bossAirConfig.BossAir.pluginNames = ["CondorPlugin"]
        bossAirConfig.section_("Agent")
        bossAirConfig.Agent.agentName = "TestAgent"

        self.localQueue = localQueue(DbName = self.localQDB,
                                     InboxDbName = self.localQInboxDB,
                                     ParentQueueCouchUrl = globalCouchUrl,
                                     JobDumpConfig = jobCouchConfig,
                                     BossAirConfig = bossAirConfig,
                                     CacheDir = self.workDir)

        self.localQueue2 = localQueue(DbName = self.localQDB2,
                                      InboxDbName = self.localQInboxDB2,
                                      ParentQueueCouchUrl = globalCouchUrl,
                                      JobDumpConfig = jobCouchConfig,
                                      BossAirConfig = bossAirConfig,
                                      CacheDir = self.workDir)

        # configuration for the Alerts messaging framework, work (alerts) and
        # control  channel addresses to which alerts
        # these are destination addresses where AlertProcessor:Receiver listens
        config = Configuration()
        config.section_("Alert")
        config.Alert.address = "tcp://127.0.0.1:5557"
        config.Alert.controlAddr = "tcp://127.0.0.1:5559"

        # standalone queue for unit tests
        self.queue = WorkQueue(JobDumpConfig = jobCouchConfig,
                               BossAirConfig = bossAirConfig,
                               DbName = self.queueDB,
                               InboxDbName = self.queueInboxDB,
                               CacheDir = self.workDir,
                               config = config)

        # create relevant sites in wmbs
        rc = ResourceControl()
        for site, se in self.queue.SiteDB.mapping.items():
            rc.insertSite(site, 100, se, cmsName = site)
            daofactory = DAOFactory(package = "WMCore.WMBS",
                                    logger = threading.currentThread().logger,
                                    dbinterface = threading.currentThread().dbi)
            addLocation = daofactory(classname = "Locations.New")
            addLocation.execute(siteName = site, seName = se)


    def tearDown(self):
        """tearDown"""
        WorkQueueTestCase.tearDown(self)
        #Delete WMBSAgent config file
        EmulatorSetup.deleteConfig(self.configFile)
        EmulatorHelper.resetEmulators()
        
    
    def createResubmitSpec(self, serverUrl, couchDB, parentage = False):
        """
        _createResubmitSpec_
        Create a bogus resubmit workload.
        """
        self.site = "cmssrm.fnal.gov"
        workload = WMWorkloadHelper(WMWorkload("TestWorkload"))
        reco = workload.newTask("reco")
        workload.setOwnerDetails(name = "evansde77", group = "DMWM")

        # first task uses the input dataset
        reco.addInputDataset(primary = "PRIMARY", processed = "processed-v1", tier = "TIER1")
        reco.data.input.splitting.algorithm = "File"
        reco.data.input.splitting.include_parents = parentage
        reco.setTaskType("Processing")
        cmsRunReco = reco.makeStep("cmsRun1")
        cmsRunReco.setStepType("CMSSW")
        reco.applyTemplates()
        cmsRunRecoHelper = cmsRunReco.getTypeHelper()
        cmsRunRecoHelper.addOutputModule("outputRECO",
                                        primaryDataset = "PRIMARY",
                                        processedDataset = "processed-v2",
                                        dataTier = "TIER2",
                                        lfnBase = "/store/dunkindonuts",
                                        mergedLFNBase = "/store/kfc")
        
        dcs = DataCollectionService(url = serverUrl, database = couchDB)

        def getJob(workload):
            job = Job()
            job["task"] = workload.getTask("reco").getPathName()
            job["workflow"] = workload.name()
            job["location"] = self.site
            job["owner"] = workload.getOwner().get("name")
            job["group"] = workload.getOwner().get("group")
            return job

        testFileA = WMFile(lfn = makeUUID(), size = 1024, events = 1024, parents = ['parent1'])
        testFileA.setLocation([self.site])
        testFileA.addRun(Run(1, 1, 2))
        testFileB = WMFile(lfn = makeUUID(), size = 1024, events = 1024, parents = ['parent2'])
        testFileB.setLocation([self.site])
        testFileB.addRun(Run(1, 3, 4))
        testJobA = getJob(workload)
        testJobA.addFile(testFileA)
        testJobA.addFile(testFileB)
        
        dcs.failedJobs([testJobA])
        topLevelTask = workload.getTopLevelTask()[0]
        workload.truncate("Resubmit_TestWorkload", topLevelTask.getPathName(), 
                          serverUrl, couchDB)
                                  
        return workload

    def testProduction(self):
        """
        Enqueue and get work for a production WMSpec.
        """
        specfile = self.spec.specUrl()
        numUnit = 1
        jobSlot = [10] * numUnit # array of jobs per block
        total = sum(jobSlot)

        for _ in range(numUnit):
            self.queue.queueWork(specfile)
        self.assertEqual(numUnit, len(self.queue))

        # try to get work
        work = self.queue.getWork({'SiteDoesNotExist' : jobSlot[0]})
        self.assertEqual([], work) # not in whitelist

        work = self.queue.getWork({'T2_XX_SiteA' : 0})
        self.assertEqual([], work)
        work = self.queue.getWork({'T2_XX_SiteA' : jobSlot[0]})
        self.assertEqual(len(work), 1)

        #no more work available
        self.assertEqual(0, len(self.queue.getWork({'T2_XX_SiteA' : total})))


    def testProductionMultiQueue(self):
        """Test production with multiple queueus"""
        specfile = self.spec.specUrl()
        numUnit = 1
        jobSlot = [10] * numUnit # array of jobs per block
        total = sum(jobSlot)

        self.globalQueue.queueWork(specfile)
        self.assertEqual(numUnit, len(self.globalQueue))

        # pull work to localQueue2 - check local doesn't get any
        self.assertEqual(numUnit, self.localQueue2.pullWork({'T2_XX_SiteA' : total}))
        self.assertEqual(0, self.localQueue.pullWork({'T2_XX_SiteA' : total}))
        syncQueues(self.localQueue)
        syncQueues(self.localQueue2)
        self.assertEqual(numUnit, len(self.localQueue2.status(status = 'Available')))
        self.assertEqual(0, len(self.localQueue.status(status = 'Available')))
        self.assertEqual(numUnit, len(self.globalQueue.status(status = 'Acquired')))
        self.assertEqual(sanitizeURL(self.localQueue2.params['QueueURL'])['url'],
                         self.globalQueue.status()[0]['ChildQueueUrl'])

#        curr_event = 1
#        for unit in work:
#            with open(unit['mask_url']) as mask_file:
#                mask = pickle.load(mask_file)
#                self.assertEqual(curr_event, mask['FirstEvent'])
#                curr_event = mask['LastEvent'] + 1
#        self.assertEqual(curr_event - 1, 10000)


    def testPriority(self):
        """
        Test priority change functionality
        """
        jobSlot = 10
        totalSlices = 1

        self.queue.queueWork(self.spec.specUrl())
        self.queue.processInboundWork()

        # priority change
        self.queue.setPriority(50, self.spec.name())
        # test elements are now cancelled
        self.assertEqual([x['Priority'] for x in self.queue.status(RequestName = self.spec.name())],
                         [50] * totalSlices)
        self.assertRaises(RuntimeError, self.queue.setPriority, 50, 'blahhhhh')

        # claim all work
        work = self.queue.getWork({'T2_XX_SiteA' : jobSlot})
        self.assertEqual(len(work), totalSlices)

        #no more work available
        self.assertEqual(0, len(self.queue.getWork({'T2_XX_SiteA' : jobSlot})))


    def testProcessing(self):
        """
        Enqueue and get work for a processing WMSpec.
        """
        specfile = self.processingSpec.specUrl()
        njobs = [5, 10] # array of jobs per block
        total = sum(njobs)

        # Queue Work & check accepted
        self.queue.queueWork(specfile)
        self.queue.processInboundWork()
        self.assertEqual(len(njobs), len(self.queue))

        self.queue.updateLocationInfo()
        # No resources
        work = self.queue.getWork({})
        self.assertEqual(len(work), 0)
        work = self.queue.getWork({'T2_XX_SiteA' : 0,
                                   'T2_XX_SiteB' : 0})
        self.assertEqual(len(work), 0)

        # Only 1 block at SiteB - get 1 work element when any resources free
        work = self.queue.getWork({'T2_XX_SiteB' : 1})
        self.assertEqual(len(work), 1)
        self.assertEqual(work[0]["NumOfFilesAdded"], GlobalParams.numOfFilesPerBlock())

        # claim remaining work
        work = self.queue.getWork({'T2_XX_SiteA' : total, 'T2_XX_SiteB' : total})
        self.assertEqual(len(work), 1)

        self.assertEqual(work[0]["NumOfFilesAdded"], GlobalParams.numOfFilesPerBlock())
        #no more work available
        self.assertEqual(0, len(self.queue.getWork({'T2_XX_SiteA' : total})))


    def testBlackList(self):
        """
        Black & White list functionality
        """
        specfile = self.blacklistSpec.specUrl()
        njobs = [5, 10] # array of jobs per block
        numBlocks = len(njobs)
        total = sum(njobs)

        # Queue Work & check accepted
        self.queue.queueWork(specfile)
        self.queue.processInboundWork()
        self.assertEqual(numBlocks, len(self.queue))
        self.queue.updateLocationInfo()

        #In blacklist (T2_XX_SiteA)
        work = self.queue.getWork({'T2_XX_SiteA' : total})
        self.assertEqual(len(work), 0)

        # copy block over to SiteB (all dbsHelpers point to same instance)

        blockLocations = {}
        blocks = DataBlockGenerator().getBlocks(self.dataset)
        for block in blocks:
            if block['Name'].endswith('1'):
                blockLocations[block['Name']] = ['T2_XX_SiteA', 'T2_XX_SiteB', 'T2_XX_SiteAA']

        Globals.moveBlock(blockLocations)
        self.queue.updateLocationInfo()

        # T2_XX_SiteA still blacklisted for all blocks
        work = self.queue.getWork({'T2_XX_SiteA' : total})
        self.assertEqual(len(work), 0)
        # SiteB can run all blocks now
        work = self.queue.getWork({'T2_XX_SiteB' : total})
        self.assertEqual(len(work), 2)

        # Test whitelist stuff
        specfile = self.whitelistSpec.specUrl()
        njobs = [5, 10] # array of jobs per block
        numBlocks = len(njobs)
        total = sum(njobs)

        self.queue.updateLocationInfo()

        # Queue Work & check accepted
        self.queue.queueWork(specfile)
        self.queue.processInboundWork()
        self.assertEqual(numBlocks, len(self.queue))

        # Only SiteB in whitelist
        work = self.queue.getWork({'T2_XX_SiteA' : total})
        self.assertEqual(len(work), 0)

        # Site B can run
        self.queue.updateLocationInfo()
        work = self.queue.getWork({'T2_XX_SiteB' : total, 'T2_XX_SiteAA' : total})
        self.assertEqual(len(work), 2)


    def testQueueChaining(self):
        """
        Chain WorkQueues, pull work down and verify splitting
        """
        self.assertEqual(0, len(self.globalQueue))
        # check no work in local queue
        self.assertEqual(0, len(self.localQueue.getWork({'T2_XX_SiteA' : 1000})))
        # Add work to top most queue
        self.globalQueue.queueWork(self.processingSpec.specUrl())
        self.assertEqual(2, len(self.globalQueue))

        # check work isn't passed down to site without subscription
        self.assertEqual(self.localQueue.pullWork({'T2_XX_SiteC' : 1000}), 0)

        # put at correct site
        self.globalQueue.updateLocationInfo()

        # check work isn't passed down to the wrong agent
        work = self.localQueue.getWork({'T2_XX_SiteC' : 1000}) # Not in subscription
        self.assertEqual(0, len(work))
        self.assertEqual(2, len(self.globalQueue))

        # pull work down to the lowest queue
        self.assertEqual(self.localQueue.pullWork({'T2_XX_SiteA' : 1000}), 2)
        syncQueues(self.localQueue)
        self.assertEqual(len(self.localQueue), 2)
        # parent state should be negotiating till we verify we have it
        #self.assertEqual(len(self.globalQueue.status('Negotiating')), 1)

        # check work passed down to lower queue where it was acquired
        # work should have expanded and parent element marked as acquired

        #self.assertEqual(len(self.localQueue.getWork({'T2_XX_SiteA' : 1000})), 0)
        # releasing on block so need to update locations
        self.localQueue.updateLocationInfo()
        work = self.localQueue.getWork({'T2_XX_SiteA' : 1000})
        self.assertEqual(0, len(self.localQueue))
        self.assertEqual(2, len(work))

        # check work in local and subscription made
        [self.assert_(x['SubscriptionId'] > 0) for x in work]
        [self.assert_(x['SubscriptionId'] > 0) for x in self.localQueue.status()]

        # mark work done & check this passes upto the top level
        self.localQueue.setStatus('Done', [x.id for x in work])


    def testQueueChainingStatusUpdates(self):
        """Chain workQueues, pass work down and verify lifecycle"""
        self.assertEqual(0, len(self.globalQueue))
        self.assertEqual(0, len(self.localQueue.getWork({'T2_XX_SiteA' : 1000})))

        # Add work to top most queue
        self.globalQueue.queueWork(self.processingSpec.specUrl())
        self.globalQueue.processInboundWork()
        self.assertEqual(2, len(self.globalQueue))

        # pull to local queue
        self.globalQueue.updateLocationInfo()
        self.assertEqual(self.localQueue.pullWork({'T2_XX_SiteA' : 1000}), 2)
        syncQueues(self.localQueue) # Tell parent local has acquired
        self.assertEqual(len(self.globalQueue.status('Acquired')), 2)
        self.assertEqual(len(self.localQueue.status('Available')), 2)

        # run work
        self.globalQueue.updateLocationInfo()
        work = self.localQueue.getWork({'T2_XX_SiteA' : 1000})
        self.assertEqual(len(work), 2)

        # resend info
        syncQueues(self.localQueue)
        self.assertEqual(len(self.globalQueue.status('Running')), 2)
        self.assertEqual(len(self.localQueue.status('Running')), 2)

        # finish work locally and propagate to global
        self.localQueue.doneWork([x.id for x in work])
        [self.localQueue.backend.updateElements(x.id, PercentComplete = 100, PercentSuccess = 99) for x in work]
        elements = self.localQueue.status('Done')
        self.assertEqual(len(elements), len(work))
        self.assertEqual([x['PercentComplete'] for x in elements],
                         [100] * len(work))
        self.assertEqual([x['PercentSuccess'] for x in elements],
                         [99] * len(work))

        self.localQueue.performQueueCleanupActions(skipWMBS = True) # will delete elements from local
        syncQueues(self.localQueue)
        
        elements = self.globalQueue.status('Done')
        self.assertEqual(len(elements), 2)
        self.assertEqual([x['PercentComplete'] for x in elements], [100,100])
        self.assertEqual([x['PercentSuccess'] for x in elements], [99, 99])

        self.globalQueue.performQueueCleanupActions()
        self.assertEqual(0, len(self.globalQueue.status()))
        elements = self.globalQueue.backend.getInboxElements('Done')
        self.assertEqual(len(elements), 1)
        self.assertEqual([x['PercentComplete'] for x in elements], [100])
        self.assertEqual([x['PercentSuccess'] for x in elements], [99])



    def testMultiTaskProduction(self):
        """
        Test Multi top level task production spec.
        multiTaskProduction spec consist 2 top level tasks each task has event size 1000 and 2000
        respectfully  
        """
        #TODO: needs more rigorous test on each element per task
        # Basic production Spec
        spec = MultiTaskProductionWorkload
        for task in spec.taskIterator():
            delattr(task.steps().data.application.configuration, 'configCacheUrl')
        spec.setSpecUrl(os.path.join(self.workDir, 'multiTaskProduction.spec'))
        spec.setOwnerDetails("evansde77", "DMWM", {'dn': 'MyDN'})
        spec.save(spec.specUrl())
        
        specfile = spec.specUrl()
        numElements = 3
        njobs = [10] * numElements # array of jobs per block
        total = sum(njobs)

        # Queue Work &njobs check accepted
        self.queue.queueWork(specfile)
        self.assertEqual(2, len(self.queue))

        # try to get work
        work = self.queue.getWork({'T2_XX_SiteA' : 0})
        self.assertEqual([], work)
        # check individual task whitelists obeyed when getting work
        work = self.queue.getWork({'T2_XX_SiteA' : total})
        self.assertEqual(len(work), 1)
        work2 = self.queue.getWork({'T2_XX_SiteB' : total})
        self.assertEqual(len(work2), 1)
        work.extend(work2)
        self.assertEqual(len(work), 2)
        self.assertEqual(sum([x['Jobs'] for x in self.queue.status(status = 'Running')]),
                         total)
        # check we have all tasks and no extra/missing ones
        for task in spec.taskIterator():
            # note: order of elements in work is undefined (both inserted simultaneously)
            element = [x for x in work if x['Subscription']['workflow'].task == task.getPathName()]
            if not element:
                self.fail("Top level task %s not in wmbs" % task.getPathName())
            element = element[0]

            # check restrictions - only whitelist for now
            whitelist = element['Subscription'].getWhiteBlackList()
            whitelist = [x['site_name'] for x in whitelist if x['valid'] == 1]
            self.assertEqual(sorted(task.siteWhitelist()), sorted(whitelist))

        #no more work available
        self.assertEqual(0, len(self.queue.getWork({'T2_XX_SiteA' : total, 'T2_XX_SiteB' : total})))
        try:
            os.unlink(specfile)
        except OSError:
            pass


    def testTeams(self):
        """
        Team behaviour
        """
        specfile = self.spec.specUrl()
        self.globalQueue.queueWork(specfile, team = 'The A-Team')
        self.globalQueue.processInboundWork()
        self.assertEqual(1, len(self.globalQueue))
        slots = {'T2_XX_SiteA' : 1000, 'T2_XX_SiteB' : 1000}

        # Can't get work for wrong team
        self.localQueue.params['Teams'] = ['other']
        self.assertEqual(self.localQueue.pullWork(slots), 0)
        # and with correct team name
        self.localQueue.params['Teams'] = ['The A-Team']
        self.assertEqual(self.localQueue.pullWork(slots), 1)
        syncQueues(self.localQueue)
        # when work leaves the queue in the agent it doesn't care about teams
        self.localQueue.params['Teams'] = ['other']
        self.assertEqual(len(self.localQueue.getWork(slots)), 1)
        self.assertEqual(0, len(self.globalQueue))

    def testMultipleTeams(self):
        """Multiple teams"""
        slots = {'T2_XX_SiteA' : 1000, 'T2_XX_SiteB' : 1000}
        self.globalQueue.queueWork(self.spec.specUrl(), team = 'The B-Team')
        self.globalQueue.queueWork(self.processingSpec.specUrl(), team = 'The C-Team')
        self.globalQueue.processInboundWork()
        self.globalQueue.updateLocationInfo()

        self.localQueue.params['Teams'] = ['The B-Team', 'The C-Team']
        self.assertEqual(self.localQueue.pullWork(slots), 3)
        syncQueues(self.localQueue)
        self.assertEqual(len(self.localQueue.getWork(slots)), 3)


    def testGlobalBlockSplitting(self):
        """Block splitting at global level"""
        # force global queue to split work on block
        self.globalQueue.params['SplittingMapping']['DatasetBlock']['name'] = 'Block'
        self.globalQueue.params['SplittingMapping']['Block']['name'] = 'Block'
        self.globalQueue.params['SplittingMapping']['Dataset']['name'] = 'Block'

        # queue work, globally for block, pass down, report back -> complete
        totalSpec = 1
        totalBlocks = totalSpec * 2
        self.assertEqual(0, len(self.globalQueue))
        for _ in range(totalSpec):
            self.globalQueue.queueWork(self.processingSpec.specUrl())
        self.globalQueue.processInboundWork()
        self.assertEqual(totalBlocks, len(self.globalQueue))
        # both blocks in global belong to same parent, but have different inputs
        status = self.globalQueue.status()
        self.assertEqual(status[0]['ParentQueueId'], status[1]['ParentQueueId'])
        self.assertNotEqual(status[0]['Inputs'], status[1]['Inputs'])

        # pull to local
        # location info should already be added
        #self.globalQueue.updateLocationInfo()
        self.assertEqual(self.localQueue.pullWork({'T2_XX_SiteA' : 1000}),
                         totalBlocks)
        syncQueues(self.localQueue)
        self.assertEqual(len(self.localQueue.status(status = 'Available')),
                         totalBlocks) # 2 in local
        #self.localQueue.updateLocationInfo()
        work = self.localQueue.getWork({'T2_XX_SiteA' : 1000, 'T2_XX_SiteB' : 1000})
        self.assertEqual(len(work), totalBlocks)
        # both refer to same wmspec
        self.assertEqual(work[0]['RequestName'], work[1]['RequestName'])
        self.localQueue.doneWork([str(x.id) for x in work])
        # elements in local deleted at end of update, only global ones left
        self.assertEqual(len(self.localQueue.status(status = 'Done')),
                         totalBlocks)
        syncQueues(self.localQueue)
        self.assertEqual(len(self.localQueue.status(status = 'Done')),
                         0)
        self.assertEqual(len(self.globalQueue.status(status = 'Done')),
                         totalBlocks)

    def testGlobalDatasetSplitting(self):
        """Dataset splitting at global level"""

        # force global queue to split work on block
        self.globalQueue.params['SplittingMapping']['DatasetBlock']['name'] = 'Dataset'
        self.globalQueue.params['SplittingMapping']['Block']['name'] = 'Dataset'
        self.globalQueue.params['SplittingMapping']['Dataset']['name'] = 'Dataset'

        # queue work, globally for block, pass down, report back -> complete
        totalSpec = 1
        totalBlocks = totalSpec * 2
        self.assertEqual(0, len(self.globalQueue))
        for _ in range(totalSpec):
            self.globalQueue.queueWork(self.processingSpec.specUrl())
        self.globalQueue.processInboundWork()
        self.assertEqual(totalSpec, len(self.globalQueue))

        # pull to local
        self.globalQueue.updateLocationInfo()
        self.assertEqual(self.localQueue.pullWork({'T2_XX_SiteA' : 1000}),
                         totalSpec)
        syncQueues(self.localQueue)
        self.assertEqual(len(self.localQueue.status(status = 'Available')),
                         totalBlocks) # 2 in local
        self.localQueue.updateLocationInfo()
        work = self.localQueue.getWork({'T2_XX_SiteA' : 1000, 'T2_XX_SiteB' : 1000})
        self.assertEqual(len(work), totalBlocks)
        # both refer to same wmspec
        self.assertEqual(work[0]['RequestName'], work[1]['RequestName'])
        self.assertNotEqual(work[0]['Inputs'], work[1]['Inputs'])
        self.localQueue.doneWork([str(x.id) for x in work])
        self.assertEqual(len(self.localQueue.status(status = 'Done')),
                         totalBlocks)
        syncQueues(self.localQueue)
        # elements in local deleted at end of update, only global ones left
        self.assertEqual(len(self.localQueue.status(status = 'Done')),
                         0)
        self.assertEqual(len(self.globalQueue.status(status = 'Done')),
                         totalSpec)

    def testResetWork(self):
        """Reset work in global to different child queue"""
        #TODO: This test sometimes fails - i suspect a race condition (maybe conflict in couch)
        # Cancel code needs reworking so this will hopefully be fixed then
        totalBlocks = 2
        self.globalQueue.queueWork(self.processingSpec.specUrl())
        self.globalQueue.updateLocationInfo()
        self.assertEqual(self.localQueue.pullWork({'T2_XX_SiteA' : 1000}),
                         totalBlocks)
        syncQueues(self.localQueue)
        work = self.localQueue.getWork({'T2_XX_SiteA' : 1000, 'T2_XX_SiteB' : 1000})
        self.assertEqual(len(work), totalBlocks)
        self.assertEqual(len(self.localQueue.status(status = 'Running')), 2)
        syncQueues(self.localQueue)
        self.assertEqual(len(self.globalQueue.status(status = 'Running')), 2)

        # Re-assign work in global
        self.globalQueue.resetWork([x.id for x in self.globalQueue.status(status = 'Running')])

        # work should be canceled in local
        #TODO: Note the work in local will be orphaned but not canceled
        syncQueues(self.localQueue)
        work_at_local = [x for x in self.globalQueue.status(status = 'Running') \
                         if x['ChildQueueUrl'] == sanitizeURL(self.localQueue.params['QueueURL'])['url']]
        self.assertEqual(len(work_at_local), 0)

        # now 2nd queue calls and acquires work
        self.assertEqual(self.localQueue2.pullWork({'T2_XX_SiteA' : 1000}),
                         totalBlocks)
        syncQueues(self.localQueue2)

        # check work in global assigned to local2
        self.assertEqual(len(self.localQueue2.status(status = 'Available')),
                         2) # work in local2
        work_at_local2 = [x for x in self.globalQueue.status(status = 'Acquired') \
                         if x['ChildQueueUrl'] == sanitizeURL(self.localQueue2.params['QueueURL'])['url']]
        self.assertEqual(len(work_at_local2), 2)


    def testCancelWork(self):
        """Cancel work"""
        self.queue.queueWork(self.processingSpec.specUrl())
        elements = len(self.queue)
        self.queue.updateLocationInfo()
        work = self.queue.getWork({'T2_XX_SiteA' : 1000, 'T2_XX_SiteB' : 1000})
        self.assertEqual(len(self.queue), 0)
        self.assertEqual(len(self.queue.status(status='Running')), elements)
        ids = [x.id for x in work]
        canceled = self.queue.cancelWork(ids)
        self.assertEqual(sorted(canceled), sorted(ids))
        self.assertEqual(len(self.queue), 0)
        self.assertEqual(len(self.queue.status()), 0)
        self.assertEqual(len(self.queue.statusInbox(status='Canceled')), 1)

        # now cancel a request
        self.queue.queueWork(self.spec.specUrl())
        elements = len(self.queue)
        work = self.queue.getWork({'T2_XX_SiteA' : 1000, 'T2_XX_SiteB' : 1000})
        self.assertEqual(len(self.queue), 0)
        self.assertEqual(len(self.queue.status(status='Running')), elements)
        ids = [x.id for x in work]
        canceled = self.queue.cancelWork(WorkflowName = ['testProduction'])
        self.assertEqual(canceled, ids)
        self.assertEqual(len(self.queue), 0)


    def testCancelWorkGlobal(self):
        """Cancel work in global queue"""
        # queue to global & pull an element to local
        self.globalQueue.queueWork(self.processingSpec.specUrl())
        self.assertEqual(self.localQueue.pullWork({'T2_XX_SiteA' : 1}), 1)
        syncQueues(self.localQueue)

        # cancel in global and propagate to local
        service = WorkQueueService(self.localQueue.backend.parentCouchUrlWithAuth)
        service.cancelWorkflow(self.processingSpec.name())
        # marked for cancel
        self.assertEqual(len(self.globalQueue.status(status='CancelRequested')), 2)
        self.assertEqual(len(self.globalQueue.statusInbox(status='Acquired')), 1)

        # will cancel element left in global, one sent to local queue stays CancelRequested
        self.globalQueue.performQueueCleanupActions()
        self.assertEqual(len(self.globalQueue.status(status='CancelRequested')), 1)
        self.assertEqual(len(self.globalQueue.status(status='Canceled')), 1)
        self.assertEqual(len(self.globalQueue.statusInbox(status='CancelRequested')), 1)
        # global parent stays CancelRequested till child queue cancels
        self.globalQueue.performQueueCleanupActions()
        self.assertEqual(len(self.globalQueue.status(status='CancelRequested')), 1)
        self.assertEqual(len(self.globalQueue.status(status='Canceled')), 1)
        self.assertEqual(len(self.globalQueue.statusInbox(status='CancelRequested')), 1)

        # during sync local will delete elements and mark inbox as canceled
        syncQueues(self.localQueue)
        self.assertEqual(len(self.localQueue.status()), 0)
        self.assertEqual(len(self.localQueue.statusInbox(status='Canceled')), 1)
        self.assertEqual(len(self.globalQueue.status(status='Canceled')), 2)
        self.assertEqual(len(self.globalQueue.statusInbox(status='CancelRequested')), 1)
        self.globalQueue.performQueueCleanupActions()
        self.assertEqual(len(self.globalQueue.status()), 0)
        self.assertEqual(len(self.globalQueue.statusInbox(status='Canceled')), 1)
        syncQueues(self.localQueue)
        # local now empty
        self.assertEqual(len(self.localQueue.statusInbox()), 0)
        # clear global
        self.globalQueue.deleteWorkflows(self.processingSpec.name())
        self.assertEqual(len(self.globalQueue.statusInbox()), 0)


    def testInvalidSpecs(self):
        """Complain on invalid WMSpecs"""
        # request != workflow name
        self.assertRaises(WorkQueueWMSpecError, self.queue.queueWork,
                                                self.processingSpec.specUrl(),
                                                request = 'fail_this')

        # invalid white list
        mcspec = monteCarloWorkload('testProductionInvalid', mcArgs)
        getFirstTask(mcspec).setSiteWhitelist('ThisIsInvalid')
        mcspec.setSpecUrl(os.path.join(self.workDir, 'testProductionInvalid.spec'))
        mcspec.save(mcspec.specUrl())
        self.assertRaises(WorkQueueWMSpecError, self.queue.queueWork, mcspec.specUrl())
        getFirstTask(mcspec).setSiteWhitelist([])
        self.queue.deleteWorkflows(mcspec.name())

        # 0 events
        getFirstTask(mcspec).addProduction(totalevents = 0)
        mcspec.save(mcspec.specUrl())
        self.assertRaises(WorkQueueNoWorkError, self.queue.queueWork, mcspec.specUrl())

        # no dataset
        processingSpec = rerecoWorkload('testProcessingInvalid', rerecoArgs)
        processingSpec.setSpecUrl(os.path.join(self.workDir,
                                                    'testProcessingInvalid.spec'))
        processingSpec.save(processingSpec.specUrl())
        getFirstTask(processingSpec).data.input.dataset = None
        processingSpec.save(processingSpec.specUrl())
        self.assertRaises(WorkQueueWMSpecError, self.queue.queueWork, processingSpec.specUrl())

        # invalid dbs url
        processingSpec = rerecoWorkload('testProcessingInvalid', rerecoArgs)
        processingSpec.setSpecUrl(os.path.join(self.workDir,
                                                    'testProcessingInvalid.spec'))
        getFirstTask(processingSpec).data.input.dataset.dbsurl = 'wrongprot://dbs.example.com'
        processingSpec.save(processingSpec.specUrl())
        self.assertRaises(WorkQueueWMSpecError, self.queue.queueWork, processingSpec.specUrl())
        self.queue.deleteWorkflows(processingSpec.name())

        # invalid dataset name
        processingSpec = rerecoWorkload('testProcessingInvalid', rerecoArgs)
        processingSpec.setSpecUrl(os.path.join(self.workDir,
                                                    'testProcessingInvalid.spec'))
        getFirstTask(processingSpec).data.input.dataset.primary = Globals.NOT_EXIST_DATASET
        processingSpec.save(processingSpec.specUrl())
        self.assertRaises(WorkQueueNoWorkError, self.queue.queueWork, processingSpec.specUrl())
        self.queue.deleteWorkflows(processingSpec.name())

        # Cant have a slash in primary ds name - validation should fail
        getFirstTask(processingSpec).data.input.dataset.primary = 'a/b'
        processingSpec.save(processingSpec.specUrl())
        self.assertRaises(WorkQueueWMSpecError, self.queue.queueWork, processingSpec.specUrl())
        self.queue.deleteWorkflows(processingSpec.name())

        # dataset splitting with invalid run whitelist
        processingSpec = rerecoWorkload('testProcessingInvalid', rerecoArgs)
        processingSpec.setSpecUrl(os.path.join(self.workDir,
                                                    'testProcessingInvalid.spec'))
        processingSpec.setStartPolicy('Dataset')
        processingSpec.setRunWhitelist([666]) # not in this dataset
        processingSpec.save(processingSpec.specUrl())
        self.assertRaises(WorkQueueNoWorkError, self.queue.queueWork, processingSpec.specUrl())
        self.queue.deleteWorkflows(processingSpec.name())

        # block splitting with invalid run whitelist
        processingSpec = rerecoWorkload('testProcessingInvalid', rerecoArgs)
        processingSpec.setSpecUrl(os.path.join(self.workDir,
                                                    'testProcessingInvalid.spec'))
        processingSpec.setStartPolicy('Block')
        processingSpec.setRunWhitelist([666]) # not in this dataset
        processingSpec.save(processingSpec.specUrl())
        self.assertRaises(WorkQueueNoWorkError, self.queue.queueWork, processingSpec.specUrl())
        self.queue.deleteWorkflows(processingSpec.name())

    def testIgnoreDuplicates(self):
        """Ignore duplicate work"""
        specfile = self.spec.specUrl()
        self.globalQueue.queueWork(specfile)
        self.assertEqual(1, len(self.globalQueue))
        
        # queue work again
        self.globalQueue.queueWork(specfile)
        self.assertEqual(1, len(self.globalQueue))


    def testConflicts(self):
        """Resolve conflicts between global & local queue"""
        self.globalQueue.queueWork(self.spec.specUrl())
        self.localQueue.pullWork({'T2_XX_SiteA' : 10000})
        self.localQueue.getWork({'T2_XX_SiteA' : 10000})
        syncQueues(self.localQueue)
        global_ids = [x.id for x in self.globalQueue.status()]
        self.localQueue.backend.updateInboxElements(*global_ids, Status = 'Done', PercentComplete = 69)
        self.globalQueue.backend.updateElements(*global_ids, Status = 'Canceled')
        self.localQueue.backend.forceQueueSync()
        self.localQueue.backend.fixConflicts()
        self.localQueue.backend.forceQueueSync()
        self.assertEqual([x['Status'] for x in self.globalQueue.status(elementIDs = global_ids)],
                         ['Canceled'])
        self.assertEqual([x['PercentComplete'] for x in self.globalQueue.status(elementIDs = global_ids)],
                         [69])
        self.assertEqual([x for x in self.localQueue.statusInbox()],
                         [x for x in self.globalQueue.status()])

    def testDeleteWork(self):
        """Delete finished work"""
        self.globalQueue.queueWork(self.spec.specUrl())
        self.assertEqual(self.localQueue.pullWork({'T2_XX_SiteA' : 10000}), 1)
        syncQueues(self.localQueue)
        self.assertEqual(len(self.localQueue.getWork({'T2_XX_SiteA' : 10000})), 1)
        syncQueues(self.localQueue)
        self.localQueue.doneWork(WorkflowName = self.spec.name())
        syncQueues(self.localQueue)
        self.assertEqual(len(self.localQueue.status(WorkflowName = self.spec.name())),
                         0) # deleted once inbox updated
        self.assertEqual('Done',
                         self.globalQueue.status(WorkflowName = self.spec.name())[0]['Status'])
        self.globalQueue.performQueueCleanupActions()
        self.assertEqual('Done',
                         self.globalQueue.statusInbox(WorkflowName = self.spec.name())[0]['Status'])
        self.assertEqual(len(self.globalQueue.status(WorkflowName = self.spec.name())),
                         0) # deleted once inbox updated
        self.globalQueue.deleteWorkflows(self.spec.name())
        self.assertEqual(len(self.globalQueue.statusInbox(WorkflowName = self.spec.name())),
                         0)
        syncQueues(self.localQueue)
        self.assertEqual(len(self.localQueue.statusInbox(WorkflowName = self.spec.name())),
                         0)
    
    def testResubmissionWorkflow(self):
        """Test workflow resubmission via ACDC"""
        acdcCouchDB = "workqueue_t_acdc"
        self.testInit.setupCouch(acdcCouchDB, "GroupUser", "ACDC")
        
        spec = self.createResubmitSpec(self.testInit.couchUrl,
                                       acdcCouchDB)
        spec.setSpecUrl(os.path.join(self.workDir, 'resubmissionWorkflow.spec'))
        spec.save(spec.specUrl())
        self.localQueue.params['Teams'] = ['cmsdataops']
        self.globalQueue.queueWork(spec.specUrl(), "Resubmit_TestWorkload", team = "cmsdataops")
        self.localQueue.pullWork({"T1_US_FNAL": 100})
        syncQueues(self.localQueue)
        self.localQueue.getWork({"T1_US_FNAL": 100})

    def testResubmissionWithParentsWorkflow(self):
        """Test workflow resubmission with parentage via ACDC"""
        acdcCouchDB = "workqueue_t_acdc"
        self.testInit.setupCouch(acdcCouchDB, "GroupUser", "ACDC")

        spec = self.createResubmitSpec(self.testInit.couchUrl,
                                       acdcCouchDB, parentage = True)
        spec.setSpecUrl(os.path.join(self.workDir, 'resubmissionWorkflow.spec'))
        spec.save(spec.specUrl())
        self.localQueue.params['Teams'] = ['cmsdataops']
        self.globalQueue.queueWork(spec.specUrl(), "Resubmit_TestWorkload", team = "cmsdataops")
        self.localQueue.pullWork({"T1_US_FNAL": 100})
        syncQueues(self.localQueue)
        self.localQueue.getWork({"T1_US_FNAL": 100})

    def testThrottling(self):
        """Pull work only if all previous work processed in child"""
        self.globalQueue.queueWork(self.processingSpec.specUrl())
        self.assertEqual(2, len(self.globalQueue))
        self.assertEqual(self.localQueue.pullWork({'T2_XX_SiteA' : 1}), 1)
        # further pull will fail till we replicate to child
        # hopefully couch replication wont happen till we manually sync
        self.assertEqual(self.localQueue.pullWork({'T2_XX_SiteA' : 1}), 0)
        self.assertEqual(1, len(self.globalQueue))
        self.assertEqual(0, len(self.localQueue))
        syncQueues(self.localQueue)
        self.assertEqual(1, len(self.localQueue))
        # pull works again
        self.assertEqual(self.localQueue.pullWork({'T2_XX_SiteA' : 1}), 1)
        
    def testSitesFromResourceControl(self):
        """Test sites from resource control"""
        # Most tests pull work for specific sites (to give us control)
        # In reality site list will come from resource control so test
        # that here (just a simple check that we can get sites from rc)
        self.globalQueue.queueWork(self.spec.specUrl())
        self.assertEqual(self.localQueue.pullWork(), 1)
        syncQueues(self.localQueue)
        self.assertEqual(len(self.localQueue.status()), 1)

    def testParentProcessing(self):
        """
        Enqueue and get work for a processing WMSpec.
        """
        specfile = self.parentProcSpec.specUrl()
        njobs = [5, 10] # array of jobs per block
        total = sum(njobs)

        # Queue Work & check accepted
        self.queue.queueWork(specfile)
        self.queue.processInboundWork()
        self.assertEqual(len(njobs), len(self.queue))

        self.queue.updateLocationInfo()
        # No resources
        work = self.queue.getWork({})
        self.assertEqual(len(work), 0)
        work = self.queue.getWork({'T2_XX_SiteA' : 0,
                                   'T2_XX_SiteB' : 0})
        self.assertEqual(len(work), 0)

        # Only 1 block at SiteB - get 1 work element when any resources free
        work = self.queue.getWork({'T2_XX_SiteB' : 1})
        self.assertEqual(len(work), 1)
        self.assertEqual(work[0]["NumOfFilesAdded"], GlobalParams.numOfFilesPerBlock() * 2)

        # claim remaining work
        work = self.queue.getWork({'T2_XX_SiteA' : total, 'T2_XX_SiteB' : total})
        self.assertEqual(len(work), 1)
        self.assertEqual(work[0]["NumOfFilesAdded"], GlobalParams.numOfFilesPerBlock() * 2)

        # no more work available
        self.assertEqual(0, len(self.queue.getWork({'T2_XX_SiteA' : total})))

    def testDrainMode(self):
        """Stop acquiring work when DrainMode set"""
        self.localQueue.params['DrainMode'] = True
        self.globalQueue.queueWork(self.spec.specUrl())
        self.assertEqual(1, len(self.globalQueue))
        self.assertEqual(self.localQueue.pullWork({'T2_XX_SiteA' : 1000, 'T2_XX_SiteB' : 1000}), 0)

    def testWMBSInjectionStatus(self):

        self.globalQueue.queueWork(self.spec.specUrl())
        self.globalQueue.queueWork(self.processingSpec.specUrl())
        # test globalqueue status (no parent queue case)
        self.assertEqual(self.globalQueue.getWMBSInjectionStatus(),
                         [{'testProcessing': False}, {'testProduction': False}])
        self.assertEqual(self.globalQueue.getWMBSInjectionStatus(self.spec.name()),
                         False)

        self.assertEqual(self.localQueue.pullWork(),3)
        # test local queue status with parents (globalQueue is not synced yet
        self.assertEqual(self.localQueue.getWMBSInjectionStatus(),
                         [{'testProcessing': False}, {'testProduction': False}])
        self.assertEqual(self.localQueue.getWMBSInjectionStatus(self.spec.name()),
                         False)
        syncQueues(self.localQueue)
        self.localQueue.processInboundWork()
        self.localQueue.updateLocationInfo()
        self.localQueue.getWork({'T2_XX_SiteA' : 1000})
        self.assertEqual(self.localQueue.getWMBSInjectionStatus(),
                            [{'testProcessing': False}, {'testProduction': False}])
        self.assertEqual(self.localQueue.getWMBSInjectionStatus(self.spec.name()),
                         False)

        #update parents status
        self.localQueue.performQueueCleanupActions()
        self.localQueue.backend.sendToParent(continuous = False)
        self.assertEqual(self.localQueue.getWMBSInjectionStatus(),
                         [{'testProcessing': True}, {'testProduction': True}])
        self.assertEqual(self.localQueue.getWMBSInjectionStatus(self.spec.name()),
                         True)

        #test not existing workflow
        self.assertRaises(ValueError,
                          self.localQueue.getWMBSInjectionStatus,
                          "NotExistWorkflow"
                         )

    def testEndPolicyNegotiating(self):
        """Test end policy processing of request before splitting"""
        work = self.globalQueue.queueWork(self.processingSpec.specUrl())
        self.assertEqual(work, 2)
        self.assertEqual(self.localQueue.pullWork({'T2_XX_SiteA' : 1}), 1)
        self.localQueue.backend.pullFromParent() # pull work into inbox (Negotiating state)
        self.localQueue.processInboundWork()
        syncQueues(self.localQueue)
        self.assertEqual(self.localQueue.pullWork({'T2_XX_SiteA' : 1}), 1)
        # should print message but not raise an error
        self.localQueue.performQueueCleanupActions(skipWMBS = True)
        self.localQueue.backend.pullFromParent(continuous = False)
        self.assertEqual(len(self.localQueue.statusInbox(Status='Negotiating')), 1)
        self.assertEqual(len(self.localQueue), 1)


    def testFailRequestAfterTimeout(self):
        """Fail a request if it errors for too long"""
        # force queue to fail queueing
        self.queue.params['SplittingMapping'] = 'thisisswrong'

        self.assertRaises(StandardError, self.queue.queueWork, self.processingSpec.specUrl())
        self.assertEqual(self.queue.statusInbox()[0]['Status'], 'Negotiating')
        # simulate time passing by making timeout negative
        self.queue.params['QueueRetryTime'] = -100
        self.assertRaises(StandardError, self.queue.queueWork, self.processingSpec.specUrl())
        self.assertEqual(self.queue.statusInbox()[0]['Status'], 'Failed')

if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python
#pylint: disable=E1103, W6501, E1101, C0301
#E1103: Use DB objects attached to thread
#W6501: Allow string formatting in error messages
#E1101: Create config sections
#C0301: The names for everything are so ridiculously long
# that I'm disabling this.  The rest of you will have to get
# bigger monitors.
"""
_AccountantWorker_

Used by the JobAccountant to do the actual processing of completed jobs.
"""

import os
import shutil
import threading
import logging
import gc
import collections

from WMCore.FwkJobReport.Report  import Report
from WMCore.DAOFactory           import DAOFactory
from WMCore.WMConnectionBase     import WMConnectionBase
from WMCore.WMException          import WMException

from WMCore.DataStructs.Run import Run
from WMCore.WMBS.File       import File
from WMCore.WMBS.Job        import Job

from WMCore.JobStateMachine.ChangeState import ChangeState
from WMComponent.DBS3Buffer.DBSBufferFile import DBSBufferFile
from WMCore.Services.PhEDEx.PhEDEx import PhEDEx
from WMCore.Services.WMStats.WMStatsWriter import WMStatsWriter
from WMCore.Database.CMSCouch import CouchServer
from WMCore.Lexicon import sanitizeURL
from WMCore.WMSpec.WMWorkload import newWorkload
from WMCore.ACDC.DataCollectionService  import DataCollectionService

class AccountantWorkerException(WMException):
    """
    _AccountantWorkerException_

    WMException based specific class
    """


class AccountantWorker(WMConnectionBase):
    """
    Class that actually does the work of parsing FWJRs for the Accountant
    Run through ProcessPool
    """
    def __init__(self, config):
        """
        __init__

        Create all DAO objects that are used by this class.
        """
        WMConnectionBase.__init__(self, "WMCore.WMBS")
        myThread = threading.currentThread()
        self.dbsDaoFactory = DAOFactory(package = "WMComponent.DBS3Buffer",
                                        logger = myThread.logger,
                                        dbinterface = myThread.dbi)

        self.getOutputMapAction      = self.daofactory(classname = "Jobs.GetOutputMap")
        self.bulkAddToFilesetAction  = self.daofactory(classname = "Fileset.BulkAddByLFN")
        self.bulkParentageAction     = self.daofactory(classname = "Files.AddBulkParentage")
        self.getJobTypeAction        = self.daofactory(classname = "Jobs.GetType")
        self.getParentInfoAction     = self.daofactory(classname = "Files.GetParentInfo")
        self.setParentageByJob       = self.daofactory(classname = "Files.SetParentageByJob")
        self.setParentageByMergeJob  = self.daofactory(classname = "Files.SetParentageByMergeJob")
        self.setFileRunLumi          = self.daofactory(classname = "Files.AddRunLumi")
        self.setFileLocation         = self.daofactory(classname = "Files.SetLocationByLFN")
        self.setFileAddChecksum      = self.daofactory(classname = "Files.AddChecksumByLFN")
        self.addFileAction           = self.daofactory(classname = "Files.Add")
        self.jobCompleteInput        = self.daofactory(classname = "Jobs.CompleteInput")
        self.setBulkOutcome          = self.daofactory(classname = "Jobs.SetOutcomeBulk")
        self.getWorkflowSpec         = self.daofactory(classname = "Workflow.GetSpecAndNameFromTask")
        self.getJobInfoByID          = self.daofactory(classname = "Jobs.LoadFromID")
        self.getFullJobInfo          = self.daofactory(classname = "Jobs.LoadForErrorHandler")
        self.getJobTaskNameAction    = self.daofactory(classname = "Jobs.GetFWJRTaskName")
        self.pnn_to_psn              = self.daofactory(classname = "Locations.GetPNNtoPSNMapping").execute()
        
        self.dbsStatusAction       = self.dbsDaoFactory(classname = "DBSBufferFiles.SetStatus")
        self.dbsParentStatusAction = self.dbsDaoFactory(classname = "DBSBufferFiles.GetParentStatus")
        self.dbsChildrenAction     = self.dbsDaoFactory(classname = "DBSBufferFiles.GetChildren")
        self.dbsCreateFiles        = self.dbsDaoFactory(classname = "DBSBufferFiles.Add")
        self.dbsSetLocation        = self.dbsDaoFactory(classname = "DBSBufferFiles.SetLocationByLFN")
        self.dbsInsertLocation     = self.dbsDaoFactory(classname = "DBSBufferFiles.AddLocation")
        self.dbsSetChecksum        = self.dbsDaoFactory(classname = "DBSBufferFiles.AddChecksumByLFN")
        self.dbsSetRunLumi         = self.dbsDaoFactory(classname = "DBSBufferFiles.AddRunLumi")
        self.dbsGetWorkflow        = self.dbsDaoFactory(classname = "ListWorkflow")
        
        self.dbsLFNHeritage      = self.dbsDaoFactory(classname = "DBSBufferFiles.BulkHeritageParent")

        self.stateChanger = ChangeState(config)

        # Decide whether or not to attach jobReport to returned value
        self.returnJobReport = getattr(config.JobAccountant, 'returnReportFromWorker', False)

        # Store location for the specs for DBS
        self.specDir = getattr(config.JobAccountant, 'specDir', None)

        # maximum RAW EDM size for Repack output before data is put into Error dataset and skips PromptReco
        self.maxAllowedRepackOutputSize = getattr(config.JobAccountant, 'maxAllowedRepackOutputSize', 12 * 1024 * 1024 * 1024)

        # ACDC service
        self.dataCollection = DataCollectionService(url = config.ACDC.couchurl,
                                                    database = config.ACDC.database)

        jobDBurl = sanitizeURL(config.JobStateMachine.couchurl)['url']
        jobDBName = config.JobStateMachine.couchDBName
        jobCouchdb  = CouchServer(jobDBurl)
        self.fwjrCouchDB = jobCouchdb.connectDatabase("%s/fwjrs" % jobDBName)
        self.localWMStats = WMStatsWriter(config.TaskArchiver.localWMStatsURL, appName="WMStatsAgent")

        # Hold data for later commital
        self.dbsFilesToCreate  = []
        self.wmbsFilesToBuild  = []
        self.wmbsMergeFilesToBuild  = []
        self.fileLocation      = None
        self.mergedOutputFiles = []
        self.listOfJobsToSave  = []
        self.listOfJobsToFail  = []
        self.filesetAssoc      = []
        self.parentageBinds    = []
        self.parentageBindsForMerge    = []
        self.jobsWithSkippedFiles = {}
        self.count = 0
        self.datasetAlgoID     = collections.deque(maxlen = 1000)
        self.datasetAlgoPaths  = collections.deque(maxlen = 1000)
        self.dbsLocations      = set()
        self.workflowIDs       = collections.deque(maxlen = 1000)
        self.workflowPaths     = collections.deque(maxlen = 1000)

        self.phedex = PhEDEx()
        self.locLists = self.phedex.getNodeMap()


        return

    def reset(self):
        """
        _reset_

        Reset all global vars between runs.
        """
        self.dbsFilesToCreate  = []
        self.wmbsFilesToBuild  = []
        self.wmbsMergeFilesToBuild  = []
        self.fileLocation      = None
        self.mergedOutputFiles = []
        self.listOfJobsToSave  = []
        self.listOfJobsToFail  = []
        self.filesetAssoc      = []
        self.parentageBinds    = []
        self.parentageBindsForMerge = []
        self.jobsWithSkippedFiles = {}
        gc.collect()
        return

    def loadJobReport(self, parameters):
        """
        _loadJobReport_

        Given a framework job report on disk, load it and return a
        FwkJobReport instance.  If there is any problem loading or parsing the
        framework job report return None.
        """
        # The jobReportPath may be prefixed with "file://" which needs to be
        # removed so it doesn't confuse the FwkJobReport() parser.
        jobReportPath = parameters.get("fwjr_path", None)
        if not jobReportPath:
            logging.error("Bad FwkJobReport Path: %s" % jobReportPath)
            return self.createMissingFWKJR(parameters, 99999, "FWJR path is empty")

        jobReportPath = jobReportPath.replace("file://","")
        if not os.path.exists(jobReportPath):
            logging.error("Bad FwkJobReport Path: %s" % jobReportPath)
            return self.createMissingFWKJR(parameters, 99999, 'Cannot find file in jobReport path: %s' % jobReportPath)

        if os.path.getsize(jobReportPath) == 0:
            logging.error("Empty FwkJobReport: %s" % jobReportPath)
            return self.createMissingFWKJR(parameters, 99998, 'jobReport of size 0: %s ' % jobReportPath)

        jobReport = Report()

        try:
            jobReport.load(jobReportPath)
        except Exception as ex:
            msg =  "Error loading jobReport %s\n" % jobReportPath
            msg += str(ex)
            logging.error(msg)
            logging.debug("Failing job: %s\n" % parameters)
            return self.createMissingFWKJR(parameters, 99997, 'Cannot load jobReport')

        if len(jobReport.listSteps()) == 0:
            logging.error("FwkJobReport with no steps: %s" % jobReportPath)
            return self.createMissingFWKJR(parameters, 99997, 'jobReport with no steps: %s ' % jobReportPath)

        return jobReport

    def isTaskExistInFWJR(self, jobReport, jobStatus):
        """
        If taskName is not available in the FWJR, then tries to
        recover it getting data from the SQL database.
        """
        if not jobReport.getTaskName():
            logging.warning("Trying to recover a corrupted FWJR for a %s job with job id %s" % (jobStatus,
                                                                                                jobReport.getJobID()))
            jobInfo = self.getJobTaskNameAction.execute(jobId = jobReport.getJobID(),
                                                        conn = self.getDBConn(),
                                                        transaction = self.existingTransaction())

            jobReport.setTaskName(jobInfo['taskName'])
            jobReport.save(jobInfo['fwjr_path'])
            if not jobReport.getTaskName():
                msg = "Report to developers. Failed to recover corrupted fwjr for %s job id %s" % (jobStatus, 
                                                                                                   jobReport.getJobID())
                raise AccountantWorkerException(msg)
            else:
                logging.info("TaskName '%s' successfully recovered and added to fwjr id %s." % (jobReport.getTaskName(),
                                                                                                jobReport.getJobID()))

        return

    def __call__(self, parameters):
        """
        __call__

        Handle a completed job.  The parameters dictionary will contain the job
        ID and the path to the framework job report.
        """
        returnList = []
        self.reset()

        for job in parameters:
            logging.info("Handling %s" % job["fwjr_path"])

            # Load the job and set the ID
            fwkJobReport = self.loadJobReport(job)
            fwkJobReport.setJobID(job['id'])
            
            jobSuccess = self.handleJob(jobID = job["id"],
                                        fwkJobReport = fwkJobReport)

            if self.returnJobReport:
                returnList.append({'id': job["id"], 'jobSuccess': jobSuccess,
                                   'jobReport': fwkJobReport})
            else:
                returnList.append({'id': job["id"], 'jobSuccess': jobSuccess})

            self.count += 1

        self.beginTransaction()

        # Now things done at the end of the job
        # Do what we can with WMBS files
        self.handleWMBSFiles(self.wmbsFilesToBuild, self.parentageBinds)
        
        # handle merge files separately since parentage need to set 
        # separately to support robust merge
        self.handleWMBSFiles(self.wmbsMergeFilesToBuild, self.parentageBindsForMerge)

        # Create DBSBufferFiles
        self.createFilesInDBSBuffer()

        # Handle filesetAssoc
        if len(self.filesetAssoc) > 0:
            self.bulkAddToFilesetAction.execute(binds = self.filesetAssoc,
                                                conn = self.getDBConn(),
                                                transaction = self.existingTransaction())

        # Move successful jobs to successful
        if len(self.listOfJobsToSave) > 0:
            idList = [x['id'] for x in self.listOfJobsToSave]
            outcomeBinds = [{'jobid': x['id'], 'outcome': x['outcome']} for x in self.listOfJobsToSave]
            self.setBulkOutcome.execute(binds = outcomeBinds,
                                    conn = self.getDBConn(),
                                    transaction = self.existingTransaction())

            self.jobCompleteInput.execute(id = idList,
                                          lfnsToSkip = self.jobsWithSkippedFiles,
                                          conn = self.getDBConn(),
                                          transaction = self.existingTransaction())
            self.stateChanger.propagate(self.listOfJobsToSave, "success", "complete")

        # If we have failed jobs, fail them
        if len(self.listOfJobsToFail) > 0:
            outcomeBinds = [{'jobid': x['id'], 'outcome': x['outcome']} for x in self.listOfJobsToFail]
            self.setBulkOutcome.execute(binds = outcomeBinds,
                                        conn = self.getDBConn(),
                                        transaction = self.existingTransaction())
            self.stateChanger.propagate(self.listOfJobsToFail, "jobfailed", "complete")

        # Arrange WMBS parentage
        if len(self.parentageBinds) > 0:
            self.setParentageByJob.execute(binds = self.parentageBinds,
                                           conn = self.getDBConn(),
                                           transaction = self.existingTransaction())
        if len(self.parentageBindsForMerge) > 0:
            self.setParentageByMergeJob.execute(binds = self.parentageBindsForMerge,
                                           conn = self.getDBConn(),
                                           transaction = self.existingTransaction())

        # Straighten out DBS Parentage
        if len(self.mergedOutputFiles) > 0:
            self.handleDBSBufferParentage()

        if len(self.jobsWithSkippedFiles) > 0:
            self.handleSkippedFiles()

        self.commitTransaction(existingTransaction = False)

        return returnList

    def outputFilesetsForJob(self, outputMap, merged, moduleLabel):
        """
        _outputFilesetsForJob_

        Determine if the file should be placed in any other fileset.  Note that
        this will not return the JobGroup output fileset as all jobs will have
        their output placed there.
        """
        if moduleLabel not in outputMap:
            logging.info("Output module label missing from output map.")
            return []

        outputFilesets = []
        for outputFileset in outputMap[moduleLabel]:
            if merged == False and outputFileset["output_fileset"] != None:
                outputFilesets.append(outputFileset["output_fileset"])
            else:
                if outputFileset["merged_output_fileset"] != None:
                    outputFilesets.append(outputFileset["merged_output_fileset"])

        return outputFilesets

    def addFileToDBS(self, jobReportFile, task, errorDataset = False):
        """
        _addFileToDBS_

        Add a file that was output from a job to the DBS buffer.
        """
        datasetInfo = jobReportFile["dataset"]

        dbsFile = DBSBufferFile(lfn = jobReportFile["lfn"],
                                size = jobReportFile["size"],
                                events = jobReportFile["events"],
                                checksums = jobReportFile["checksums"],
                                status = "NOTUPLOADED")
        dbsFile.setAlgorithm(appName = datasetInfo["applicationName"],
                             appVer = datasetInfo["applicationVersion"],
                             appFam = jobReportFile["module_label"],
                             psetHash = "GIBBERISH",
                             configContent = jobReportFile.get('configURL'))

        if errorDataset:
            dbsFile.setDatasetPath("/%s/%s/%s" % (datasetInfo["primaryDataset"] + "-Error",
                                                  datasetInfo["processedDataset"],
                                                  datasetInfo["dataTier"]))
        else:
            dbsFile.setDatasetPath("/%s/%s/%s" % (datasetInfo["primaryDataset"],
                                                  datasetInfo["processedDataset"],
                                                  datasetInfo["dataTier"]))

        dbsFile.setValidStatus(validStatus = jobReportFile.get("validStatus", None))
        dbsFile.setProcessingVer(ver = jobReportFile.get('processingVer', None))
        dbsFile.setAcquisitionEra(era = jobReportFile.get('acquisitionEra', None))
        dbsFile.setGlobalTag(globalTag = jobReportFile.get('globalTag', None))
        #TODO need to find where to get the prep id
        dbsFile.setPrepID(prep_id = jobReportFile.get('prep_id', None))
        dbsFile['task'] = task

        for run in jobReportFile["runs"]:
            newRun = Run(runNumber = run.run)
            newRun.extend(run.lumis)
            dbsFile.addRun(newRun)


        dbsFile.setLocation(pnn = list(jobReportFile["locations"])[0], immediateSave = False)
        self.dbsFilesToCreate.append(dbsFile)
        return

    def findDBSParents(self, lfn):
        """
        _findDBSParents_

        Find the parent of the file in DBS
        This is meant to be called recursively
        """
        parentsInfo = self.getParentInfoAction.execute([lfn],
                                                       conn = self.getDBConn(),
                                                       transaction = self.existingTransaction())
        newParents = set()
        for parentInfo in parentsInfo:
            # This will catch straight to merge files that do not have redneck
            # parents.  We will mark the straight to merge file from the job
            # as a child of the merged parent.
            if int(parentInfo["merged"]) == 1:
                newParents.add(parentInfo["lfn"])

            elif parentInfo['gpmerged'] == None:
                continue

            # Handle the files that result from merge jobs that aren't redneck
            # children.  We have to setup parentage and then check on whether or
            # not this file has any redneck children and update their parentage
            # information.
            elif int(parentInfo["gpmerged"]) == 1:
                newParents.add(parentInfo["gplfn"])

            # If that didn't work, we've reached the great-grandparents
            # And we have to work via recursion
            else:
                parentSet = self.findDBSParents(lfn = parentInfo['gplfn'])
                for parent in parentSet:
                    newParents.add(parent)

        return newParents

    def addFileToWMBS(self, jobType, fwjrFile, jobMask, task, jobID = None):
        """
        _addFileToWMBS_

        Add a file that was produced in a job to WMBS.
        """
        fwjrFile["first_event"] = jobMask["FirstEvent"]

        if fwjrFile["first_event"] == None:
            fwjrFile["first_event"] = 0

        if jobType == "Merge" and fwjrFile["module_label"] != "logArchive":
            setattr(fwjrFile["fileRef"], 'merged', True)
            fwjrFile["merged"] = True

        wmbsFile = self.createFileFromDataStructsFile(file = fwjrFile, jobID = jobID)
        
        if jobType == "Merge":
            self.wmbsMergeFilesToBuild.append(wmbsFile)
        else:
            self.wmbsFilesToBuild.append(wmbsFile)
            
        if fwjrFile["merged"]:
            self.addFileToDBS(fwjrFile, task,
                              jobType == "Repack" and fwjrFile["size"] > self.maxAllowedRepackOutputSize)

        return wmbsFile


    def _mapLocation(self, fwkJobReport):
        for file in fwkJobReport.getAllFileRefs():
            if file and hasattr(file, 'location'):
                file.location = self.phedex.getBestNodeName(file.location, self.locLists)


    def handleJob(self, jobID, fwkJobReport):
        """
        _handleJob_

        Figure out if a job was successful or not, handle it appropriately
        (parse FWJR, update WMBS) and return the success status as a boolean

        """
        jobSuccess = fwkJobReport.taskSuccessful()

        outputMap = self.getOutputMapAction.execute(jobID = jobID,
                                                    conn = self.getDBConn(),
                                                    transaction = self.existingTransaction())

        jobType = self.getJobTypeAction.execute(jobID = jobID,
                                                conn = self.getDBConn(),
                                                transaction = self.existingTransaction())

        if jobSuccess:
            fileList = fwkJobReport.getAllFiles()

            # consistency check comparing outputMap to fileList
            # they should match except for some limited special cases
            outputModules = set([])
            for fwjrFile in fileList:
                outputModules.add(fwjrFile['outputModule'])
            if set(outputMap.keys()) == outputModules:
                pass
            elif jobType == "LogCollect" and len(outputMap.keys()) == 0 and outputModules == set(['LogCollect']):
                pass
            elif jobType == "Merge" and set(outputMap.keys()) == set(['Merged', 'MergedError', 'logArchive']) and outputModules == set(['Merged', 'logArchive']):
                pass
            elif jobType == "Merge" and set(outputMap.keys()) == set(['Merged', 'MergedError', 'logArchive']) and outputModules == set(['MergedError', 'logArchive']):
                pass
            elif jobType == "Express" and set(outputMap.keys()).difference(outputModules) == set(['write_RAW']):
                pass
            else:
                failJob = True
                if jobType in [ "Processing", "Production" ]:
                    cmsRunSteps = 0
                    for step in fwkJobReport.listSteps():
                        if step.startswith("cmsRun"):
                            cmsRunSteps += 1
                    if cmsRunSteps > 1:
                        failJob = False

                if failJob:
                    jobSuccess = False
                    logging.error("Job %d , list of expected outputModules does not match job report, failing job", jobID)
                    logging.debug("Job %d , expected outputModules %s", jobID, sorted(outputMap.keys()))
                    logging.debug("Job %d , fwjr outputModules %s", jobID, sorted(outputModules))
                    fileList = fwkJobReport.getAllFilesFromStep(step = 'logArch1')
                else:
                    logging.debug("Job %d , list of expected outputModules does not match job report, accepted for multi-step CMSSW job", jobID)
        else:
            fileList = fwkJobReport.getAllFilesFromStep(step = 'logArch1')

        if jobSuccess:
            logging.info("Job %d , handle successful job", jobID)
        else:
            logging.error("Job %d , bad jobReport, failing job",  jobID)

        # make sure the task name is present in FWJR (recover from WMBS if needed)
        if len(fileList) > 0:
            if jobSuccess:
                self.isTaskExistInFWJR(fwkJobReport, "success")
            else:
                self.isTaskExistInFWJR(fwkJobReport, "failed")

        # special check for LogCollect jobs
        skipLogCollect = False
        if jobSuccess and jobType == "LogCollect":
            for fwjrFile in fileList:
                try:
                    # this assumes there is only one file for LogCollect jobs, not sure what happend if that changes
                    self.associateLogCollectToParentJobsInWMStats(fwkJobReport, fwjrFile["lfn"], fwkJobReport.getTaskName())
                except Exception as ex:
                    skipLogCollect = True
                    logging.error("Error occurred: associating log collect location, will try again\n %s" % str(ex))
                    break

        # now handle the job (unless the special LogCollect check failed)
        if not skipLogCollect:

            wmbsJob = Job(id = jobID)
            wmbsJob.load()
            outputID = wmbsJob.loadOutputID()
            wmbsJob.getMask()

            wmbsJob["fwjr"] = fwkJobReport

            if jobSuccess:
                wmbsJob["outcome"] = "success"
            else:
                wmbsJob["outcome"] = "failure"

            for fwjrFile in fileList:

                logging.debug("Job %d , register output %s", jobID, fwjrFile["lfn"])

                wmbsFile = self.addFileToWMBS(jobType, fwjrFile, wmbsJob["mask"],
                                              jobID = jobID, task = fwkJobReport.getTaskName())
                merged = fwjrFile['merged']
                moduleLabel = fwjrFile["module_label"]

                if merged:
                    self.mergedOutputFiles.append(wmbsFile)

                self.filesetAssoc.append({"lfn": wmbsFile["lfn"], "fileset": outputID})

                # LogCollect jobs have no output fileset
                if jobType == "LogCollect":
                    pass
                # Repack jobs that wrote too large merged output skip output filesets
                elif jobType == "Repack" and merged and wmbsFile["size"] > self.maxAllowedRepackOutputSize:
                    pass
                else:
                    outputFilesets = self.outputFilesetsForJob(outputMap, merged, moduleLabel)
                    for outputFileset in outputFilesets:
                        self.filesetAssoc.append({"lfn": wmbsFile["lfn"], "fileset": outputFileset})

            # Check if the job had any skipped files, put them in ACDC containers
            # We assume full file processing (no job masks)
            if jobSuccess:
                skippedFiles = fwkJobReport.getAllSkippedFiles()
                if skippedFiles:
                    self.jobsWithSkippedFiles[jobID] = skippedFiles

            # Only save once job is done, and we're sure we made it through okay
            self._mapLocation(wmbsJob['fwjr'])
            if jobSuccess:
                self.listOfJobsToSave.append(wmbsJob)
            else:
                self.listOfJobsToFail.append(wmbsJob)

        return jobSuccess
    
    def associateLogCollectToParentJobsInWMStats(self, fwkJobReport, logAchiveLFN, task):
        """
        _associateLogCollectToParentJobsInWMStats_

        Associate a logArchive output to its parent job
        """
        inputFileList = fwkJobReport.getAllInputFiles()
        requestName = task.split('/')[1]
        keys = []
        for inputFile in inputFileList:
            keys.append([requestName, inputFile["lfn"]])
        resultRows = self.fwjrCouchDB.loadView("FWJRDump", 'jobsByOutputLFN', 
                                               options = {"stale": "update_after"},
                                               keys = keys)['rows']
        if len(resultRows) > 0:
            #get data from wmbs
            parentWMBSJobIDs = []
            for row in resultRows:
                parentWMBSJobIDs.append({"jobid": row["value"]})
            #update Job doc in wmstats
            results = self.getJobInfoByID.execute(parentWMBSJobIDs)
            parentJobNames = []
            
            if isinstance(results, list):
                for jobInfo in results:
                    parentJobNames.append(jobInfo['name'])
            else:
                parentJobNames.append(results['name'])
            
            self.localWMStats.updateLogArchiveLFN(parentJobNames, logAchiveLFN)
        else:
            #TODO: if the couch db is consistent with DB this should be removed (checking resultRow > 0)
            #It need to be failed and retried.
            logging.error("job report is missing for updating log archive mapping\n Input file list\n %s" % inputFileList)

        return

    def createMissingFWKJR(self, parameters, errorCode = 999,
                           errorDescription = 'Failure of unknown type'):
        """
        _createMissingFWJR_

        Create a missing FWJR if the report can't be found by the code in the
        path location.
        """
        report = Report()
        report.addError("cmsRun1", 84, errorCode, errorDescription)
        report.data.cmsRun1.status = "Failed"
        return report

    def createFilesInDBSBuffer(self):
        """
        _createFilesInDBSBuffer_
        It does the actual job of creating things in DBSBuffer
        WARNING: This assumes all files in a job have the same final location
        """
        if len(self.dbsFilesToCreate) == 0:
            # Whoops, nothing to do!
            return

        dbsFileTuples = []
        dbsFileLoc    = []
        dbsCksumBinds = []
        runLumiBinds  = []
        selfChecksums = None
        jobLocations  = set()

        for dbsFile in self.dbsFilesToCreate:
            # Append a tuple in the format specified by DBSBufferFiles.Add
            # Also run insertDatasetAlgo

            assocID         = None
            datasetAlgoPath = '%s:%s:%s:%s:%s:%s:%s:%s' % (dbsFile['datasetPath'],
                                                           dbsFile["appName"],
                                                           dbsFile["appVer"],
                                                           dbsFile["appFam"],
                                                           dbsFile["psetHash"],
                                                           dbsFile['processingVer'],
                                                           dbsFile['acquisitionEra'],
                                                           dbsFile['globalTag'])
            # First, check if this is in the cache
            if datasetAlgoPath in self.datasetAlgoPaths:
                for da in self.datasetAlgoID:
                    if da['datasetAlgoPath'] == datasetAlgoPath:
                        assocID = da['assocID']
                        break

            if not assocID:
                # Then we have to get it ourselves
                try:
                    assocID = dbsFile.insertDatasetAlgo()
                    self.datasetAlgoPaths.append(datasetAlgoPath)
                    self.datasetAlgoID.append({'datasetAlgoPath': datasetAlgoPath,
                                               'assocID': assocID})
                except WMException:
                    raise
                except Exception as ex:
                    msg =  "Unhandled exception while inserting datasetAlgo: %s\n" % datasetAlgoPath
                    msg += str(ex)
                    logging.error(msg)
                    raise AccountantWorkerException(msg)

            # Associate the workflow to the file using the taskPath and the requestName
            # TODO: debug why it happens and then drop/recover these cases automatically
            taskPath = dbsFile.get('task')
            if not taskPath:
                msg = "Can't do workflow association, report this error to a developer.\n"
                msg += "DbsFile : %s" % str(dbsFile)
                raise AccountantWorkerException(msg)
            workflowName = taskPath.split('/')[1]
            workflowPath = '%s:%s' % (workflowName, taskPath)
            if workflowPath in self.workflowPaths:
                for wf in self.workflowIDs:
                    if wf['workflowPath'] == workflowPath:
                        workflowID = wf['workflowID']
                        break
            else:
                result = self.dbsGetWorkflow.execute(workflowName, taskPath, conn = self.getDBConn(),
                                                         transaction = self.existingTransaction())
                workflowID = result['id']

            self.workflowPaths.append(workflowPath)
            self.workflowIDs.append({'workflowPath': workflowPath, 'workflowID': workflowID})

            lfn           = dbsFile['lfn']
            selfChecksums = dbsFile['checksums']
            jobLocation   = dbsFile.getLocations()[0]
            jobLocations.add(jobLocation)
            dbsFileTuples.append((lfn, dbsFile['size'],
                                  dbsFile['events'], assocID,
                                  dbsFile['status'], workflowID))

            dbsFileLoc.append({'lfn': lfn, 'sename' : jobLocation})
            if dbsFile['runs']:
                runLumiBinds.append({'lfn': lfn, 'runs': dbsFile['runs']})

            if selfChecksums:
                # If we have checksums we have to create a bind
                # For each different checksum
                for entry in selfChecksums.keys():
                    dbsCksumBinds.append({'lfn': lfn, 'cksum' : selfChecksums[entry],
                                          'cktype' : entry})

        try:
            
            diffLocation = jobLocations.difference(self.dbsLocations)

            for jobLocation in diffLocation:
                self.dbsInsertLocation.execute(siteName = jobLocation,
                                               conn = self.getDBConn(),
                                               transaction = self.existingTransaction())
                self.dbsLocations.add(jobLocation)

            self.dbsCreateFiles.execute(files = dbsFileTuples,
                                        conn = self.getDBConn(),
                                        transaction = self.existingTransaction())

            self.dbsSetLocation.execute(binds = dbsFileLoc,
                                        conn = self.getDBConn(),
                                        transaction = self.existingTransaction())

            self.dbsSetChecksum.execute(bulkList = dbsCksumBinds,
                                        conn = self.getDBConn(),
                                        transaction = self.existingTransaction())

            if len(runLumiBinds) > 0:
                self.dbsSetRunLumi.execute(file = runLumiBinds,
                                           conn = self.getDBConn(),
                                           transaction = self.existingTransaction())
        except WMException:
            raise
        except Exception as ex:
            msg =  "Got exception while inserting files into DBSBuffer!\n"
            msg += str(ex)
            logging.error(msg)
            logging.debug("Listing binds:")
            logging.debug("jobLocation: %s\n" % jobLocation)
            logging.debug("dbsFiles: %s\n" % dbsFileTuples)
            logging.debug("dbsFileLoc: %s\n" %dbsFileLoc)
            logging.debug("Checksum binds: %s\n" % dbsCksumBinds)
            logging.debug("RunLumi binds: %s\n" % runLumiBinds)
            raise AccountantWorkerException(msg)


        # Now that we've created those files, clear the list
        self.dbsFilesToCreate = []
        return


    def handleWMBSFiles(self, wmbsFilesToBuild, parentageBinds):
        """
        _handleWMBSFiles_

        Do what can be done in bulk in bulk
        """
        if len(wmbsFilesToBuild) == 0:
            # Nothing to do
            return

        runLumiBinds   = []
        fileCksumBinds = []
        fileLocations  = []
        fileCreate     = []

        for wmbsFile in wmbsFilesToBuild:
            lfn           = wmbsFile['lfn']
            if lfn == None:
                continue

            selfChecksums = wmbsFile['checksums']
            # by jobType add to different parentage relation
            # if it is the merge job, don't include the parentage on failed input files.
            # otherwise parentage is set for all input files.
            parentageBinds.append({'child': lfn, 'jobid': wmbsFile['jid']})
                
            if wmbsFile['runs']:
                runLumiBinds.append({'lfn': lfn, 'runs': wmbsFile['runs']})

            if len(wmbsFile.getLocations()) > 0:
                outpnn = wmbsFile.getLocations()[0]
                if self.pnn_to_psn.get(outpnn, None):
                    fileLocations.append({'lfn': lfn, 'location': outpnn})
                else:
                    msg = "PNN doesn't exist in wmbs_location_sename table: %s (investigate)" % outpnn
                    logging.error(msg)
                    raise AccountantWorkerException(msg)

            if selfChecksums:
                # If we have checksums we have to create a bind
                # For each different checksum
                for entry in selfChecksums.keys():
                    fileCksumBinds.append({'lfn': lfn, 'cksum' : selfChecksums[entry],
                                           'cktype' : entry})

            fileCreate.append([lfn,
                               wmbsFile['size'],
                               wmbsFile['events'],
                               None,
                               wmbsFile["first_event"],
                               wmbsFile['merged']])

        if len(fileCreate) == 0:
            return

        try:

            self.addFileAction.execute(files = fileCreate,
                                       conn = self.getDBConn(),
                                       transaction = self.existingTransaction())

            if runLumiBinds:
                self.setFileRunLumi.execute(file = runLumiBinds,
                                            conn = self.getDBConn(),
                                            transaction = self.existingTransaction())

            self.setFileAddChecksum.execute(bulkList = fileCksumBinds,
                                            conn = self.getDBConn(),
                                            transaction = self.existingTransaction())

            self.setFileLocation.execute(lfn = fileLocations,
                                         location = self.fileLocation,
                                         conn = self.getDBConn(),
                                         transaction = self.existingTransaction())


        except WMException:
            raise
        except Exception as ex:
            msg =  "Error while adding files to WMBS!\n"
            msg += str(ex)
            logging.error(msg)
            logging.debug("Printing binds: \n")
            logging.debug("FileCreate binds: %s\n" % fileCreate)
            logging.debug("Runlumi binds: %s\n" % runLumiBinds)
            logging.debug("Checksum binds: %s\n" % fileCksumBinds)
            logging.debug("FileLocation binds: %s\n" % fileLocations)
            raise AccountantWorkerException(msg)

        # Clear out finished files
        wmbsFilesToBuild = []
        return

    def createFileFromDataStructsFile(self, file, jobID):
        """
        _createFileFromDataStructsFile_

        This function will create a WMBS File given a DataStructs file
        """
        wmbsFile = File()
        wmbsFile.update(file)

        if isinstance(file["locations"], set):
            pnn = list(file["locations"])[0]
        elif isinstance(file["locations"], list):
            if len(file['locations']) > 1:
                logging.error("Have more then one location for a file in job %i" % (jobID))
                logging.error("Choosing location %s" % (file['locations'][0]))
            pnn = file["locations"][0]
        else:
            pnn = file["locations"]

        wmbsFile["locations"] = set()

        if pnn != None:
            wmbsFile.setLocation(pnn = pnn, immediateSave = False)
        wmbsFile['jid'] = jobID
        
        return wmbsFile

    def handleDBSBufferParentage(self):
        """
        _handleDBSBufferParentage_

        Handle all the DBSBuffer Parentage in bulk if you can
        """
        outputLFNs = [f['lfn'] for f in self.mergedOutputFiles]
        bindList         = []
        for lfn in outputLFNs:
            newParents = self.findDBSParents(lfn = lfn)
            for parentLFN in newParents:
                bindList.append({'child': lfn, 'parent': parentLFN})

        # Now all the parents should exist
        # Commit them to DBSBuffer
        logging.info("About to commit all DBSBuffer Heritage information")
        logging.info(len(bindList))

        if len(bindList) > 0:
            try:
                self.dbsLFNHeritage.execute(binds = bindList,
                                            conn = self.getDBConn(),
                                            transaction = self.existingTransaction())
            except WMException:
                raise
            except Exception as ex:
                msg =  "Error while trying to handle the DBS LFN heritage\n"
                msg += str(ex)
                msg += "BindList: %s" % bindList
                logging.error(msg)
                raise AccountantWorkerException(msg)
        return

    def handleSkippedFiles(self):
        """
        _handleSkippedFiles_

        Handle all the skipped files in bulk,
        the way it handles the skipped files
        imposes an important restriction:
        Skipped files should have been processed by a single job
        in the task and no job mask exists in it.
        This is suitable for jobs using ParentlessMergeBySize/FileBased/MinFileBased
        splitting algorithms.
        Here ACDC records and created and the file are moved
        to wmbs_sub_files_failed from completed.
        """
        jobList = self.getFullJobInfo.execute([{'jobid' : x} for x in self.jobsWithSkippedFiles.keys()],
                                              fileSelection = self.jobsWithSkippedFiles,
                                              conn = self.getDBConn(),
                                              transaction = self.existingTransaction())
        self.dataCollection.failedJobs(jobList, useMask = False)
        return

# -*- coding: utf-8 -*-
"""
/***************************************************************************
 ZonalExactDialog
                                 A QGIS plugin
 Zonal Statistics of rasters using Exact Extract library
 Generated by Plugin Builder: http://g-sherman.github.io/Qgis-Plugin-Builder/
                             -------------------
        begin                : 2024-02-11
        git sha              : $Format:%H$
        copyright            : (C) 2024 by Jakub Charyton
        email                : jakub.charyton@gmail.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

import os
import sys
from typing import Dict, List
from pathlib import Path

from qgis.PyQt import uic
from qgis.PyQt import QtWidgets, QtCore
from qgis.PyQt.QtCore import QVariant
from qgis.core import (QgsMapLayerProxyModel, QgsFieldProxyModel, QgsTask, QgsTaskManager, QgsMessageLog, QgsVectorLayer, 
                    QgsFeatureRequest, QgsVectorLayerJoinInfo, QgsRasterLayer, QgsMapLayer, QgsWkbTypes)

import pandas as pd

from .dialog_input_dto import DialogInputDTO
from .user_communication import UserCommunication, WidgetPlainTextWriter
from .task_classes import CalculateStatsTask, MergeStatsTask
from .widgets.codeEditor import CodeEditorUI
from .utils import extract_function_name

# This loads your .ui file so that PyQt can populate your plugin with the elements from Qt Designer
FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'zonal_exact_dialog_base.ui'))

default_code = """import numpy as np

def np_mean(values, cov):
    average_value=np.average(values, weights=cov)
    return average_value
"""

class ZonalExactDialog(QtWidgets.QDialog, FORM_CLASS):
    def __init__(self, parent=None, uc: UserCommunication = None, iface = None, project = None, task_manager: QgsTaskManager = None):
        """Constructor."""
        super(ZonalExactDialog, self).__init__(parent)
        # Set up the user interface from Designer through FORM_CLASS.
        # After self.setupUi() you can access any designer object by doing
        # self.<objectname>, and you can use autoconnect slots - see
        # http://qt-project.org/doc/qt-4.8/designer-using-a-ui-file.html
        # #widgets-and-dialogs-with-auto-connect
        # Initiate  a new instance of the dialog input DTO class to hold all input data
        self.dialog_input: DialogInputDTO = None
        # Initiate an empty list for storing tasks in queue
        self.tasks = []
        # Initiate an empty list to store intermediate results of zonal statistics calculation
        self.intermediate_result_list = []
        # Initiate main task that will hold aggregated data from child calculating tasks
        self.merge_task: MergeStatsTask = None
        self.output_attribute_layer = None
        self.calculated_stats_list = []
        self.temp_index_field = None
        self.input_vector = None
        self.features_count = None
        self.custom_functions_dict: Dict[str, str] = {}  # it holds custom functions and should reflect mCustomFunctionsComboBox content
        # assign qgis internal variables to class variables
        self.uc = uc
        self.iface = iface
        self.project = project
        self.task_manager: QgsTaskManager = task_manager
        
        self.editor = CodeEditorUI(default_code)
        self.editor.resize(600, 300)
        self.editor.setWindowTitle("Custom Function Code Editor")
        
        self.setupUi(self)
        
        self.set_id_field()
        
        self.widget_console = WidgetPlainTextWriter(self.mPlainText)
        
        # set filters on combo boxes to get correct layer types
        self.mWeightsLayerComboBox.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.mVectorLayerComboBox.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        # set ID field combo box to current vector layer
        self.mFieldComboBox.setFilters(QgsFieldProxyModel.LongLong | QgsFieldProxyModel.Int)
        if self.mVectorLayerComboBox.currentLayer():
            self.mFieldComboBox.setLayer(self.mVectorLayerComboBox.currentLayer())
        self.mVectorLayerComboBox.layerChanged.connect(self.set_field_vector_layer)
        # set temp_index_field class variable when user selects another index field
        if self.mFieldComboBox.currentField():
            self.temp_index_field = self.mFieldComboBox.currentField()
        self.mFieldComboBox.fieldChanged.connect(self.set_id_field)
        # set output file allowed extensions
        self.mQgsOutputFileWidget.setFilter("Documents (*.csv *.parquet)")
        # make weights layer empty as default
        self.mWeightsLayerComboBox.setCurrentIndex(0)
        
        self.mCalculateButton.clicked.connect(self.calculate)
        
        self.mAddModifyMetricButton.clicked.connect(self.edit_metric_function)
        self.editor.codeSubmitted.connect(self.modify_code)

    def calculate(self):
        """
        The calculate method disables the calculate button, gets input values from the dialog 
        and stores them in the dialog_input attribute, and initiates the calculation process 
        using QgsTask and exactextract. If an exception occurs during the calculation, 
        an error message is logged and displayed in the console.
        """
        self.mCalculateButton.setEnabled(False)
        try:
            self.get_input_values()  # loads input values from the dialog into self.dialog_input
            if self.dialog_input is None:
                self.mCalculateButton.setEnabled(True)
                return
            self.input_vector: QgsVectorLayer = self.dialog_input.vector_layer
            
            self.features_count = self.input_vector.featureCount()
            batch_size = round(self.features_count / self.dialog_input.parallel_jobs)
            
            # calculate using QgsTask and exactextract
            self.process_calculations(self.input_vector, batch_size)
            
            # wait for calculations to finish to continue
            if self.merge_task is not None:
                self.merge_task.taskCompleted.connect(self.postprocess)
        except ValueError as exc:
            QgsMessageLog.logMessage(f'ERROR: {str(exc)}')
            self.uc.bar_warn(str(exc))
            self.widget_console.write_error(str(exc))
        finally:
            if self.input_vector:
                self.input_vector.removeSelection()  # remove selection of features after processing
            self.mCalculateButton.setEnabled(True)
            
    def process_calculations(self, vector: QgsVectorLayer, batch_size: int):
        """
        Processes the calculations for zonal statistics using exactextract.
        This method initiates a series of tasks to calculate zonal statistics for a given vector layer
        using exactextract. It creates a `CalculateStatsTask` for each batch of features and adds it
        as a subtask to a `MergeStatsTask`.

        Args:
            vector (QgsVectorLayer): The input vector layer for which to calculate zonal statistics.
            batch_size (int): The number of features to process in each batch.
        """
        self.intermediate_result_list = []
        self.merge_task = MergeStatsTask(f'Zonal ExactExtract task', QgsTask.CanCancel, widget_console=self.widget_console,
                                                    result_list=self.intermediate_result_list,
                                                    index_column=self.temp_index_field, prefix=self.dialog_input.prefix)
        self.merge_task.taskCompleted.connect(self.update_progress_bar)
        
        self.tasks = []
        
        vector.selectAll()
        feature_ids = vector.selectedFeatureIds()
        vector.removeSelection()
        for i in range(0, self.features_count, batch_size):
            if self.dialog_input.parallel_jobs == 1:
                temp_vector = vector
            else:
                selection_ids = feature_ids[i : i + batch_size]
                vector.selectByIds(selection_ids)
                
                # Create a new memory layer with the same geometry type and fields structure as the source layer
                crs = vector.crs()
                fields = vector.fields()
                geom_type = vector.geometryType()
                temp_vector = QgsVectorLayer(
                    QgsWkbTypes.geometryDisplayString(geom_type) +
                    "?crs=" + crs.authid() + "&index=yes",
                    "Memory layer",
                    "memory"
                )
                memoryLayerDataProvider = temp_vector.dataProvider()
                # copy the table structure
                temp_vector.startEditing()
                memoryLayerDataProvider.addAttributes(fields)
                temp_vector.commitChanges()
                # Add selected features to the new memory layer
                memoryLayerDataProvider.addFeatures(vector.selectedFeatures())
                # temp_vector = vector.materialize(QgsFeatureRequest().setFilterFids(vector.selectedFeatureIds()))
            
            stats_list = self.dialog_input.aggregates_stats_list+self.dialog_input.arrays_stats_list+self.dialog_input.custom_functions_list
            calculation_subtask = CalculateStatsTask(f'calculation subtask {i}', flags=QgsTask.Silent, result_list=self.intermediate_result_list,
                                                    widget_console=self.widget_console, polygon_layer=temp_vector, 
                                                    rasters=self.dialog_input.raster_layers_path, weights=self.dialog_input.weights_layer_path, 
                                                    stats=stats_list, include_cols=[self.temp_index_field])
            calculation_subtask.taskCompleted.connect(self.update_progress_bar)
            self.tasks.append(calculation_subtask)
            self.merge_task.addSubTask(calculation_subtask, [], QgsTask.ParentDependsOnSubTask)

        self.task_manager.addTask(self.merge_task)
        
    def postprocess(self):
        """
        This method is called after the zonal statistics calculation is complete. It saves the result 
        to a file based on the user's selected file format, loads the output into QGIS, and joins the 
        output to the input vector layer if necessary.
        """
        try:
            calculated_stats = self.merge_task.calculated_stats
            QgsMessageLog.logMessage(f'Zonal ExactExtract task result shape: {str(calculated_stats.shape)}')
            self.widget_console.write_info(f'Zonal ExactExtract task result shape: {str(calculated_stats.shape)}')
            
            # save result based on user decided extension
            if self.dialog_input.output_file_path.suffix == '.csv':
                calculated_stats.to_csv(self.dialog_input.output_file_path, index=False)
            elif self.dialog_input.output_file_path.suffix == '.parquet':
                calculated_stats.to_parquet(self.dialog_input.output_file_path, index=False)
            
            # load output into QGIS
            output_attribute_layer = QgsVectorLayer(str(self.dialog_input.output_file_path), Path(self.dialog_input.output_file_path).stem, 'ogr')
            # check if the layer was loaded successfully
            if not output_attribute_layer.isValid():
                QgsMessageLog.logMessage(f'Unable to load layer from {self.dialog_input.output_file_path}')
                self.widget_console.write_error(f'Unable to load layer from {self.dialog_input.output_file_path}')
            else:
                self.widget_console.write_info('Finished calculating statistics')
                # Add the layer to the project
                self.project.addMapLayer(output_attribute_layer)
                self.output_attribute_layer = output_attribute_layer
                
                if self.mJoinCheckBox.isChecked():
                    self.create_join()
            
        except Exception as exc:
            QgsMessageLog.logMessage(f'ERROR: {exc}')
            self.widget_console.write_error(exc)
        finally:
            self.clean()
            self.mCalculateButton.setEnabled(True)

    def create_join(self):
        """
        This method creates a join between the output attribute layer and the input vector layer based on a 
        common index field. It is called after the output attribute layer has been loaded into QGIS.
        """
        joinObject = QgsVectorLayerJoinInfo()
        joinObject.setJoinLayer(self.output_attribute_layer)
        joinObject.setJoinFieldName(self.temp_index_field)
        joinObject.setTargetFieldName(self.temp_index_field)
        joinObject.setUsingMemoryCache(True)
        if not self.input_vector.addJoin(joinObject):
            QgsMessageLog.logMessage("Can't join output to input layer")
            self.widget_console.write_error("Can't join output to input layer")
            
    def update_progress_bar(self):
        """
        Calculate progress change as percentage of total tasks completed + parent task
        """
        progress_change = int((1 / (len(self.tasks) + 1)) * 100)
        self.mProgressBar.setValue(self.mProgressBar.value() + progress_change)
        
    def clean(self):
        """
        Reinitialize object values to free memory after calculation is done
        """
        self.dialog_input: DialogInputDTO = None
        self.tasks = []
        self.intermediate_result_list = []
        self.merge_task: MergeStatsTask = None
        self.calculated_stats_list = []
        
        self.mProgressBar.setValue(0)
        
    def get_input_values(self):
        """
        Gets input values from dialog and puts it into `DialogInputDTO` class object.
        """
        raster_layers_path: List[QgsRasterLayer] = self.extract_layers_path(self.mRasterLayersList.checked_layers())
        weights_layer_path: str = None
        if self.mWeightsLayerComboBox.currentLayer():
            weights_layer_path = self.mWeightsLayerComboBox.currentLayer().dataProvider().dataSourceUri()
        vector_layer: QgsVectorLayer = self.mVectorLayerComboBox.currentLayer()
        parallel_jobs: int = self.mSubtasksSpinBox.value()
        if self.mQgsOutputFileWidget.filePath() == '': 
            output_file_path = None
        else: 
            output_file_path: Path = Path(self.mQgsOutputFileWidget.filePath())
        aggregates_stats_list: List[str] = self.mAggregatesComboBox.checkedItems()
        arrays_stats_list: List[str] = self.mArraysComboBox.checkedItems()
        prefix: str = self.mPrefixEdit.text()
        
        try:
            self.control_input(raster_layers_path=raster_layers_path, vector_layer=vector_layer, 
                            output_file_path=output_file_path, aggregates_stats_list=aggregates_stats_list, arrays_stats_list=arrays_stats_list)
        except ValueError as exc:
            raise exc  # there's been error during control of the input values and we can't push processing further
        
        # create list with custom functions codes that will be converted to callables
        custom_functions: List[str] = []
        selected_functions_names: List[str] = self.mCustomFunctionsComboBox.checkedItems()
        if selected_functions_names:
            for selected_function_name in selected_functions_names:
                custom_functions.append(self.custom_functions_dict[selected_function_name])
        
        self.dialog_input = DialogInputDTO(raster_layers_path=raster_layers_path, weights_layer_path=weights_layer_path,  vector_layer=vector_layer, 
                                        parallel_jobs=parallel_jobs, output_file_path=output_file_path, aggregates_stats_list=aggregates_stats_list, 
                                        arrays_stats_list=arrays_stats_list, prefix=prefix, custom_functions_str_list=custom_functions)
    
    def extract_layers_path(self, layers: List[QgsMapLayer]):
        """
        This method extracts the data source URIs of the input map layers and returns a list of the extracted URIs.

        Args:
            layers: List[QgsMapLayer] - A list of QGIS map layers.

        Returns:
            List[str] - A list of the data source URIs of the input map layers.
        """
        layers_path: List[str] = []
        for layer in layers:
            layers_path.append(layer.dataProvider().dataSourceUri())
        return layers_path
    
    def control_input(self, raster_layers_path, vector_layer, output_file_path, aggregates_stats_list, arrays_stats_list):
        """
        Processes the input data by checking the validity of the input parameters.

        This method checks if both raster and vector layers are set, if the ID field is set, if the ID field is unique, if an output
        file path is selected, if the output file extension is CSV or Parquet, and if both stats lists are empty.

        Args:
            raster_layers_path: Path - The path to the raster layer.
            vector_layer: QgsVectorLayer - The vector layer.
            temp_index_field: str - The ID field.
            output_file_path: Path - The path to the output file.
            aggregates_stats_list: List[str] - The list of aggregates statistics.
            arrays_stats_list: List[str] - The list of arrays statistics.
        """
        # check if both raster and vector layers are set
        if not raster_layers_path or not vector_layer:
            err_msg = f"You didn't select raster layer or vector layer"
            raise ValueError(err_msg)
        # check if ID field is set
        if not self.temp_index_field:
            err_msg = f"You didn't select ID field"
            raise ValueError(err_msg)
        # check if ID field is unique
        # TODO: Checking uniqueness would require a looping over all features in the vector layer, which is slow and may take a lot of time
        # depending on size of the input dataset therefore it is omitted for now until we have a better solution
        # We might add a checkbox to let user decide wether we should check uniqueness (with given information that it might be slow operation)
        if not output_file_path:
            err_msg = f"You didn't select output file path"
            raise ValueError(err_msg)
        # check if output file extension is CSV or Parquet
        if output_file_path.suffix != ".csv" and output_file_path.suffix != '.parquet':
            err_msg = f"Allowed output formats are CSV (.csv) or Parquet (.parquet)"
            raise ValueError(err_msg)
        else:
            if output_file_path.suffix == '.parquet':
                try:
                    import fastparquet
                except ImportError:
                    err_msg = f"Parquet output format is supported only if fastparquet library is installed"
                    raise ValueError(err_msg)
        # check if both stats lists are empty
        if not aggregates_stats_list and not arrays_stats_list:
            err_msg = f"You didn't select anything from either Aggregates and Arrays"
            raise ValueError(err_msg)
        # array output statistics are not proper for fastparquet 
        # check if there are array output statistics to be calculated when using parquet as an output format
        if output_file_path.suffix == '.parquet' and arrays_stats_list:
            err_msg = f'Array stats: {",".join(arrays_stats_list)} are forbidden in conjuction with .parquet output format'
            raise ValueError(err_msg)
            
    
    def set_field_vector_layer(self):
        """
        Sets fields to the Field ComboBox if vector layer has changed
        """
        selectedLayer = self.mVectorLayerComboBox.currentLayer()
        if selectedLayer:
            self.mFieldComboBox.setLayer(selectedLayer)
    
    def set_id_field(self):
        """
        Sets index method variable 
        """
        self.temp_index_field = self.mFieldComboBox.currentField()
        
    def edit_metric_function(self):
        """
        Edits the metric function by setting the editor to the selected custom function code or the default code.

        This method retrieves the topmost checked custom function from the combobox, gets the corresponding code from the
        custom_functions_dict, and sets the editor to display that code. If no item is selected or the list is empty, the
        default code is used instead.
        """
        try:
            function_name = self.mCustomFunctionsComboBox.checkedItems()[0]
            code = self.custom_functions_dict[function_name]
        except IndexError:  # no item selected or list is empty
            code = default_code
        # set editor to that code
        self.editor.set_code(code)
        self.editor.show()
    
    def modify_code(self, code: str):
        """
        Modifies the code in the custom functions dictionary and updates the combobox

        Args:
            code: The code to be modified and added to the dictionary.
        """
        # get function name as string
        function_name = extract_function_name(code)
        # modify the code in the dict
        self.custom_functions_dict[function_name] = code
        # if function name does not exist in combobox add function to combobox
        if self.mCustomFunctionsComboBox.findText(function_name) == -1:
            self.mCustomFunctionsComboBox.addItemWithCheckState(function_name, QtCore.Qt.Checked)
        